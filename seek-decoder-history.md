# Seek 与解码器历史：责任边界与精确定位限制

本文梳理 PyAV 中一次 `container.seek(...)` 调用之后，从定位请求、首个解码画面到逐帧
到达目标画面的完整链路，说明各层（调用方代码、PyAV 绑定、libavformat、libavcodec）
分别负责什么，以及为什么 seek 不能直接“落在”目标帧上。

涉及的主要代码：

| 角色 | 位置 |
| --- | --- |
| 定位入口与解码器刷新编排 | `av/container/input.py`（`InputContainer.seek`、`InputContainer.flush_buffers`） |
| 解码器状态重置 | `av/codec/context.py`（`CodecContext.flush_buffers`、`CodecContext._decode`） |
| 每流解码器与 `pkt_timebase` 绑定 | `av/container/input.py`（`__cinit__`）、`av/stream.py` |
| 包的解码分发与时基携带 | `av/packet.py`、`av/video/stream.py`、`av/audio/stream.py` |
| 帧时基赋值 | `av/codec/context.py`（`_setup_decoded_frame`）、`av/frame.py` |
| 索引查询（定位前预查关键帧） | `av/index.py`（`IndexEntries.search_timestamp`） |
| 行为测试样例 | `tests/test_seek.py` |

---

## 1. 定位请求：谁负责把读指针移到目标附近

### 1.1 `InputContainer.seek` 做了什么

`av/container/input.py:319` 的 `seek(offset, *, backward=True, any_frame=False, stream=None, ...)`
是唯一的定位入口，它只做两件事：

1. 组装标志位并调用 FFmpeg 的 `av_seek_frame`（`av/container/input.py:377-379`）：

   ```python
   stream_index: cython.int = stream.index if stream else -1
   with cython.nogil:
       ret = lib.av_seek_frame(self.ptr, stream_index, c_offset, flags)
   self.err_check(ret)
   ```

   - `backward=True`（默认）→ `AVSEEK_FLAG_BACKWARD`：目标点没有关键帧时，定位到
     **不晚于 offset 的最近关键帧**（`input.py:365-366`）。
   - `any_frame=False`（默认）→ 不带 `AVSEEK_FLAG_ANY`，即只接受关键帧。
   - `AVSEEK_FLAG_FRAME` / `AVSEEK_FLAG_BYTE` 对应 `unsupported_frame_offset` /
     `unsupported_byte_offset`，参数名本身就写明“没有已知格式支持”，仅供实验
     （`input.py:370-374`）。
   - `offset` 必须是 `int`，传浮点直接抛 `TypeError`（`input.py:358-359`，测试见
     `tests/test_seek.py:15`）；`int64` 大值也要能容纳（`test_seek_int64`）。

2. 调用 `self.flush_buffers()` 刷新**所有流**的解码器（`input.py:381`，详见第 3 节）。

### 1.2 offset 的时基由调用方负责，PyAV 不做换算

`av_seek_frame` 收到的是裸整数时间戳，PyAV 不做任何 rescale。时基取决于
`stream_index`：

- 不传 `stream`（`stream_index = -1`，“默认流/容器级”定位）：`offset` 必须以
  `av.time_base` 为单位，即 `AV_TIME_BASE`（微秒，`av/_core.py:7`）。
  `tests/test_seek.py:96-101` 的 `test_decode_half` 就是先把目标帧号换算成微秒再 seek。
- 传 `stream=`（`stream_index = 该流 index`）：`offset` 必须以 **该流自己的
  `stream.time_base`** 为单位。`test_stream_seek`（`tests/test_seek.py:120-137`）
  用 `target_sec / time_base + stream.start_time` 手工构造时间戳。

这是多流场景的第一个坑：每条 `AVStream` 的 `time_base` 相互独立
（`av/stream.py:255-263`），视频可能是 1/12800，音频可能是 1/48000。用错时基不会报错，
只会定位到错误的位置。

### 1.3 seek 的责任边界

`seek()` 的契约写在 docstring 里（`input.py:345-351`）：demux 出来的包只是
**“大致（roughly）”**对应请求位置；默认参数之所以是“向后 + 仅关键帧”，是因为视频帧
要正确解码，必须从前一个关键帧开始。因此：

- **libavformat 负责**：移动 demuxer/IO 读指针、按索引找到满足标志约束的关键帧位置、
  清空 demuxer 自身的包队列（`av_seek_frame` 内部行为）。
- **PyAV 负责**：参数到标志位的翻译、错误码转异常（`err_check`）、以及随后刷新解码器。
- **调用方负责**：选对 offset 的时基、接受“落点是关键帧而非目标帧”、并在解码循环里
  跳过落点到目标之间的帧（见第 2、4 节）。

相关的容器级开关在 `av/container/core.py:171-183`：`fast_seek`
（`AVFMT_FLAG_FAST_SEK`，快但不准）、`no_parse`（关闭解析器后“按帧定位无法工作”），
这些限制来自 demuxer，PyAV 只是透传。

### 1.4 不可定位输入与自定义 IO

- 管道、socket 或没有 `seek`/`tell` 的 file-like 对象不可 seek
  （`av/container/pyio.py:34-41`、`input.py:142-143`）；对这类输入调用 seek 会由
  FFmpeg 返回错误，经 `err_check` 抛异常。
- 可定位性还取决于格式是否建了索引。`av/index.py` 的 `IndexEntries` 是 demuxer 提供的
  每流索引，可能为空或不完整（`index.py:78-86`）；`search_timestamp`
  （`index.py:134-155`，包装 `av_index_search_timestamp`）可在 seek 前预查关键帧位置，
  适合“多次定位、只抽少数帧”的循环，但索引缺失时返回 -1。

---

## 2. 首个解码画面：为什么不是请求的那一帧

### 2.1 demux 恢复读包

seek 之后需要**重新开始迭代** `container.demux(...)`（`input.py:203-301`）。
demux 循环每次 `av_read_frame` 前会 `av_packet_unref` 复用读缓冲
（`input.py:259-264`），并把命中的包 `av_packet_move_ref` 成新的 `Packet` 交给调用方，
同时打上该流的时基：

```python
packet._stream = self.streams[packet.ptr.stream_index]
packet.ptr.time_base = packet._stream.ptr.time_base   # input.py:283-285
```

默认（`backward=True, any_frame=False`）下，恢复后读到的**第一个视频包通常就是落点
关键帧的包**，它的时间戳 ≤ 请求的 offset。多流同时 demux 时，音频/字幕流会各自从
“与新位置一致的各自最近点”恢复，不同流的首个包时间戳并不对齐。

### 2.2 包到帧的解码链

`Packet.decode()` 直接转发给所属流的解码器（`av/packet.py:322-327`）：
`VideoStream.decode` / `AudioStream.decode` 都是 `self.cctx.decode(packet)` 的薄封装
（`av/video/stream.py:150-160`、`av/audio/stream.py:120-130`）。

核心在 `CodecContext._decode`（`av/codec/context.py:762-788`），是现代 FFmpeg 的
send/receive 模型：

1. `avcodec_send_packet` 喂入一个包（`context.py:770-774`）；
2. 循环 `avcodec_receive_frame`（经 `_recv_frame`，`context.py:648-667`）直到
   `EAGAIN`/`EOF`；
3. 每收到一帧，`_setup_decoded_frame` 给帧赋时基（`context.py:802-812`）。

两个直接影响 seek 体验的特性：

- **一个包可能产出 0 个或多个帧，一个帧也可能由多个包产生。** 落点关键帧的包喂进去后，
  首个 `VideoFrame` 未必在同一次 `decode()` 调用里返回。
- **B 帧重排。** 解码器按 DTS 吃包、按 PTS 出帧，内部持有延迟帧
  （`has_b_frames` / `reorder_depth`，`av/video/codeccontext.py:360-379`）。所以“首个
  解码画面”在呈现顺序上对应落点关键帧或其后的帧，而不是请求点。

### 2.3 帧时基来自包，而包时基来自流

`_setup_decoded_frame`（`context.py:802-812`）：

```python
if packet is not None:
    frame._time_base = packet.ptr.time_base          # 正常解码：包携带的流时基
else:
    frame._time_base = self.ptr.pkt_timebase         # 直接 decode(None) 冲刷时
```

每个流的解码器在容器打开时就绑定了 `codec_context.pkt_timebase = stream.time_base`
（`input.py:82`）。因此跨流比较视频帧和音频帧的时间戳时，必须先各自换算（例如用
`frame.time` 转秒，`av/frame.py:166-168`），不能直接比较整数 PTS——这是多流时基影响
目标画面判断的第二层含义。

### 2.4 调用方如何识别“到目标了”

`tests/test_seek.py` 给出了标准模式（`test_decode_half`，83-118 行）：

1. 用 `timestamp_to_frame`（`tests/test_seek.py:6-11`）把首帧 PTS 换算成帧序号；
2. seek 后逐帧计数，**在 `current_frame >= target_frame` 之前的帧全部丢弃**；
3. 断言从目标帧到结尾的帧数恰好等于 `总帧数 - 目标帧号`，证明落点在目标之前且解码连续。

即：**定位请求只保证“起点 ≤ 目标”，首个解码画面是关键帧，逐帧推进到目标是调用方循环
的责任。**

---

## 3. 清理旧状态：seek 后为什么必须 flush，以及 flush 清了什么

### 3.1 seek 自动触发全流刷新

`InputContainer.seek` 在 `av_seek_frame` 成功后无条件调用 `self.flush_buffers()`
（`input.py:381`）。其实现遍历容器内**每一条流**（`input.py:383-393`）：

```python
for stream in self.streams:
    codec_context = stream.cctx
    if codec_context:
        codec_context.flush_buffers()
```

注意几点：

- 没有解码器的流（打开时 `avcodec_find_decoder` 失败，`input.py:88-90` 令 `cctx=None`）
  自然跳过。
- 刷新是**全流**的，即使这次 seek 只锚定其中一条流（`stream=`）。因为 demuxer 位置
  跳变影响所有流，音频解码器若不刷新，会把 seek 前的音频样本和 seek 后的包混着拼。
- 该方法是 `@cython.cfunc`（`input.pxd:8`），不暴露给 Python；用户能直接调用的是
  每个 `CodecContext.flush_buffers()`（`context.py:790-800`，`cpdef` 级公开 API），
  docstring 明确写着“在新位置开始解码前，例如 seek 或切换流时调用”。

### 3.2 `avcodec_flush_buffers` 清除与保留的状态

`CodecContext.flush_buffers`（`context.py:790-800`）：

```python
if lib.avcodec_is_open(self.ptr):
    with cython.nogil:
        lib.avcodec_flush_buffers(self.ptr)
```

- 尚未 `open` 的解码器直接跳过（懒打开发生在首次 `_decode`，`context.py:767`）。
- 清除的是 libavcodec 的**内部状态**：已缓存的参考帧、B 帧重排队列、延迟输出、
  预测相关的内部缓冲。不刷新的话，seek 后第一个 GOP 的帧会引用 seek 前的参考帧，
  产出花屏/错位画面——这就是“解码器历史影响目标画面”的根因。
- **不清除**的东西：codec 参数（宽高、像素格式、extradata、`pkt_timebase` 等
  配置）、Python 侧持有的 `Packet`/`Frame` 对象、demuxer 的状态（由
  `av_seek_frame` 自己处理）、以及 `AVCodecParser` 实例（parser 在
  `CodecContext.parse` 路径下独立存在，`context.py:492-497`，与容器 demux 解码路径
  分开；手动用 parser 的用户需要自行处理其缓冲）。

### 3.3 flush 自身的限制

1. **重排启发式可能被重置。** `VideoCodecContext.reorder_depth`
   （`av/video/codeccontext.py:367-379`）的 docstring 明确警告：flush 之后 FFmpeg 可能
   重置内部重排序启发式；对含层级 B 帧（hierarchical B）的流，需要在 seek **之后**把
   已知的重排深度写回 `reorder_depth`，否则可能丢帧。这是 flush 不保证“无副作用重置”
   的直接证据。
2. **个别解码器 flush 是空操作。** `Flags2.ro_flush_noop`
   （`AV_CODEC_FLAG2_RO_FLUSH_NOOP`，`context.py:102`）标记按设计 flush 无效的只读
   解码器；这类 codec 下旧状态的处理方式不同。
3. **音频 seek 预滚（preroll）。** `CodecContext.seek_preroll`
   （`context.py:1177-1183`）表示不连续点之后要跳过多少样本输出才正确；单位是样本数，
   由 codec/容器参数决定，flush 本身不替用户消费这些样本。
4. **线程安全。** `decode()` 不是线程安全的（`context.py:751-757` 的警告），多线程共用
   一个 `CodecContext` 会破坏内部状态；seek+flush 与解码并发同样受此约束，每线程应持有
   独立上下文。
5. **字幕解码器走另一条 API。** `SubtitleCodecContext._decode` 用的是
   `avcodec_decode_subtitle2`（`av/subtitles/codeccontext.py:95-132`），flush 语义靠反复
   发送空包实现；它同样继承 `flush_buffers()`，但内部模型与音视频的 send/receive 不同。

### 3.4 demux 生成器与 flush 虚拟包的交互

`demux()` 在正常 EOF 时会为每条选中的流 yield 一个**空的虚拟包**用于冲刷解码器
（`input.py:288-295`）。seek 场景需要注意：

- seek 应当在**没有悬挂的 demux 生成器**时调用，或 seek 后丢弃旧生成器、重新迭代。
  生成器在 `yield` 处挂起期间不会感知位置变化；恢复后它仍用旧的 `include_stream`
  选择集合继续 `av_read_frame`（循环开头有“容器可能已关闭”的检查，
  `input.py:253-256`，但没有“位置已跳变”的检查）。
- seek 内部的 `flush_buffers` 已经清掉解码器积压，因此 seek 后**不应**再依赖旧生成器
  末尾的虚拟 flush 包；新一次 `demux()` 走到 EOF 时会产生自己的虚拟包。

---

## 4. 逐帧到达目标：调用方循环的责任

综合第 1–3 节，一次精确到帧的定位由三层分工完成：

| 步骤 | 负责方 | 代码/机制 |
| --- | --- | --- |
| 选择时基并构造整数 offset | 调用方 | `av.time_base` 微秒，或 `stream.time_base` 单位 |
| 移动 demuxer 到最近关键帧 | libavformat（经 `av_seek_frame`） | `input.py:377-379`，`BACKWARD` + 仅关键帧 |
| 清空所有解码器的历史帧/重排队列 | PyAV → libavcodec | `input.py:381-393` → `context.py:790-800` |
| 重新 demux 出落点关键帧起的包 | 调用方驱动，libavformat 供包 | `input.py:253-286` |
| 逐包解码、容忍 0/N 帧产出与 B 帧重排 | libavcodec | `context.py:762-788` |
| 按 PTS/帧号丢弃落点到目标之间的帧 | 调用方 | `tests/test_seek.py:106-117` 的计数模式 |
| 跨流对齐（音视频/字幕） | 调用方 | 各流时基独立，需换算到统一时钟 |

实用要点：

- **判断目标帧要用呈现时间（PTS）而非解码顺序。** `Frame.pts` 配合流时基，或直接用
  `Frame.time`（秒）；`frame.dts`（`av/frame.py:89-91`）只在无帧线程时可作近似。
- **多流同步要先统一时基。** 视频流、音频流的 `time_base` 不同（`stream.py:255-263`），
   seek 的 offset 时基与后续帧的时基也可能不同（微秒 offset vs 流时基 PTS），任何
  “视频帧对应哪段音频”的判断都必须 rescale。
- **不关心的流提前 `discard`。** `Stream.discard`（`stream.py:316-327`）可让 demuxer
  跳过该流，docstring 明确提到这样 `demux` 和 `seek` 都不必为不同步的流付代价；
  MPEG-TS 里一个流属于多个 program 时要所有 program 都 discard 才会真正丢包
  （`input.py:492-506`）。
- **需要反复随机抽帧时，先查索引。** `stream.index_entries.search_timestamp`
  （`index.py:134-155`）能在不实际读包的情况下找到关键帧条目，再决定 seek 参数；
  但索引可能不完整，-1 时只能回退到普通 seek。

---

## 5. 精确定位的限制清单

以下限制均源自容器/编解码器格式或 FFmpeg 架构，PyAV 无法在绑定层消除：

1. **关键帧粒度。** 默认 seek 只能落在关键帧；目标帧与关键帧之间的距离（GOP 长度）
   决定要解码并丢弃多少帧。`any_frame=True`（`AVSEEK_FLAG_ANY`）允许落在非关键帧，
   但该帧缺少参考帧，解码输出无效，仍要等到下一个关键帧才能得到正确画面。
2. **不保证 offset 精确匹配。** docstring 用词是 “roughly”（`input.py:345`）；
   `test_seek_end`（`tests/test_seek.py:61-81`）验证了无法真正 seek 到结尾，只能到
   最后一个关键帧。
3. **时基必须调用方匹配。** PyAV 不 rescale offset（`input.py:361-378`）；流级 seek 用
   流时基，容器级 seek 用 `AV_TIME_BASE` 微秒。多流各自独立的 `time_base` 还要求帧级
   比较前自行换算。
4. **格式/索引依赖。** 索引缺失或不完整（`index.py:78-86`）、demuxer 只支持粗粒度定位、
   `AVFMT_FLAG_FAST_SEEK` 的快速但不精确模式（`core.py:183`）、关闭 parser 后帧边界
   定位失效（`core.py:176`），都是格式层差异。
5. **不可定位介质。** 管道、实时流、无 seek 能力的 file-like 不能定位
   （`pyio.py:34-41`）；这类源只能靠从头解码/外部关键帧请求（如 RTSP）实现近似跳转。
6. **flush 不是万能重置。** 它清解码器缓冲但不重置重排启发式（可能需要手工恢复
   `reorder_depth`，`video/codeccontext.py:371-374`）；部分解码器 flush 为 no-op
   （`ro_flush_noop`）；音频有 `seek_preroll` 样本要跳过；手动 `parse()` 的 parser
   缓冲不在自动清理范围内。
7. **API 面收窄。** PyAV 只暴露 `av_seek_frame` 的简化封装（`include/avformat.pxd:226`
   声明了但未直接暴露更细的 `avformat_seek_file`），按字节/按帧号定位标记为
   “unsupported”（`input.py:340-343, 370-374`）；offset 仅接受整数。
8. **延迟解码与帧线程。** B 帧导致输出延迟、send/receive 的 EAGAIN 缓冲、帧线程下
   `dts` 语义受限，意味着“喂入落点包”和“拿到首个有效帧”之间没有固定帧数关系，
   调用方循环必须以 PTS 为准而不能假设固定偏移。
9. **并发约束。** 同一 `CodecContext` 上 seek/flush 与 decode 并发会破坏状态
   （`context.py:751-757`）；需要并发时为每条执行链路独立打开容器或解码器上下文。

---

## 附：一次典型精确定位的调用时序

```text
调用方                InputContainer            libavformat            libavcodec
  |  seek(ts_us)          |                         |                      |
  |---------------------->| av_seek_frame(...)      |                      |
  |                       |------------------------>| 移动IO/找关键帧/     |
  |                       |                         | 清demux队列          |
  |                       | flush_buffers() 遍历全流 |                      |
  |                       |---------------------------------------------->| avcodec_flush_buffers
  |                       |                         |        清参考帧/B帧队列/延迟帧
  |  for pkt in demux()   |                         |                      |
  |---------------------->| av_read_frame --------->| 落点关键帧包(pts<=ts)|
  |  pkt.decode()         | cctx._decode            |                      |
  |                       | send_packet/receive_frame ------------------>|
  |  <VideoFrame pts...>  | 帧时基=包时基=流时基     |                      |
  |  按 PTS 跳过早期帧     |                         |                      |
  |  直到 pts >= 目标      |                         |                      |
```
