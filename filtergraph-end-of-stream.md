# 多输入滤镜图的结束语义

本文只依据仓库内代码与测试材料分析 PyAV 的 `av.filter` 绑定（`av/filter/*.py`、`av/filter/*.pxd`、`include/avfilter.pxd`），不运行媒体服务，也不调用真实解码器。

证据等级标记：

- 【代码】= 直接引自本仓库实现，附 `file:line`。
- 【测试】= 由本仓库测试断言或注释支撑，附 `file:line`。
- 【待验证】= 涉及 FFmpeg 内部（buffersrc/framesync/具体滤镜）行为，本仓库既无 vendored 源码也无对应测试；只给出可执行的验证判据，不作结论。

FFmpeg 侧源码不在仓库内（`vendor/` 不存在，`include/avfilter.pxd` 只有 C 声明），所以凡是"某个滤镜在部分输入结束时会怎样"的断言一律归入【待验证】，但"绑定层能表达什么"是确定的。

---

## 1. 绑定层暴露的输入/输出面

### 1.1 源与汇的识别

`wrap_filter_context()` 按滤镜名把上下文分成四类【代码】(`av/filter/context.py:28-37`)：

```python
name = filter.name
if name == "buffer" or name == "abuffer": self._kind = _KIND_SOURCE
elif name == "buffersink":                self._kind = _KIND_VIDEO_SINK
elif name == "abuffersink":               self._kind = _KIND_AUDIO_SINK
else:                                     self._kind = _KIND_OTHER
```

因此"输入"在调用层就是 `buffer`/`abuffer` 上下文，是一个**逐上下文**的概念，不是图的整体属性。

### 1.2 推送与拉取的两个入口

- `Graph.push(frame, at=-1)` / `Graph.vpush(frame, at=-1)`【代码】(`av/filter/graph.py:273-319`)：按类型选出源集合，`at` 选其中一个，`at=-1` 广播。
- `FilterContext.push(frame)`【代码】(`av/filter/context.py:105-132`)：源上下文直接写；非源上下文**沿唯一输入回溯委托**。
- `Graph.pull()`/`vpull()`【代码】(`av/filter/graph.py:322-339`)：要求**全局恰好一个汇**；否则 `ValueError`。
- `FilterContext.pull()`【代码】(`av/filter/context.py:134-160`)：汇上下文直接 `av_buffersink_get_frame()`；非汇上下文沿唯一输出委托。

`Graph.pull` 的单汇限制在 TODO 里已经承认【代码】(`av/filter/graph.py:321`)：

```python
# TODO: Test complex filter graphs, add `at: int = 0` arg to pull() and vpull().
```

这意味着**多汇图（例如同时产出视频与音频）只能走 `FilterContext.pull()`，`Graph.pull()` 直接拒绝**。

### 1.3 各输入的时间基是逐源声明、逐源独立的

- `add_buffer(..., time_base=None)`：缺省时**猜测 `1/1000` 并发 `DeprecationWarning`**【代码】(`av/filter/graph.py:194-207`)。
- `add_abuffer(..., time_base=None)`：缺省时用 `1/sample_rate`【代码】(`av/filter/graph.py:243-249`)。
- `template=` 时直接取 `template.time_base`【代码】(`av/filter/graph.py:185-186`, `235`; `_template_ctx()` 在 `16-20` 行把"流的 format/layout 透传"改读 `cctx`）。
- 该值以字符串形式进入 `buffer`/`abuffer` 的参数：`time_base=str(time_base)`【代码】(`av/filter/graph.py:207`, `249`）。

**关键**：`AVFrame` 在 FFmpeg 里没有 `time_base` 字段（`include/avcodec.pxd:372-402` 的 `AVFrame` 结构里只有 `pts`/`pkt_dts`/`duration`，无 `time_base`），PyAV 把时间基存在 Python 侧 `Frame._time_base`【代码】(`av/frame.pxd:9`)。所以 `av_buffersrc_write_frame(self.ptr, frame.ptr)`【代码】(`av/filter/context.py:119-121`) 传下去的只是裸 `pts`，**其单位完全由该源声明的 `time_base` 解释**。绑定层在这一步：

- **不做重标定**（push 路径没有任何 `_rebase_time`/`av_rescale_q`；对比编码路径 `_prepare_and_time_rebase_frames_for_encode()` 明确会 `frame._rebase_time(self.ptr.time_base)`【代码】(`av/codec/context.py:706-713`)）；
- **不校验** `frame.time_base` 与源声明是否一致（`push` 里没有任何比较）。

推论【代码】：调用者必须保证"帧的 `pts` 已经是该源 `time_base` 下的值"。若用了 `add_buffer()` 的缺省 `1/1000` 却按 `1/30` 的 pts 推送，帧在图里会被当作 1/1000 单位的毫秒值，后续所有输出时点整体错位，且**不会报任何错**。仓库里只有 `tests/test_videoframe.py:242-248` 显式传 `time_base=Fraction(1, 1000)` 与 `tests/test_filters.py:375-378` 两源同为 `1/30`，没有跨时间基的用例。

### 1.4 输出的时间基来自"汇的输入链接"

`FilterContext.pull()` 在拿到帧后这样定时间基【代码】(`av/filter/context.py:154-160`)：

```python
res = lib.av_buffersink_get_frame(self.ptr, frame.ptr)
err_check(res)
frame._init_user_attributes()
frame.time_base = from_avrational(self.ptr.inputs[0].time_base)
```

即：**输出帧的时间基是配置后 `abuffersink`/`buffersink` 第 0 个输入链接协商出的 `AVFilterLink.time_base`**，不是输入源声明的值。链接时间基由 FFmpeg 与相邻滤镜决定【待验证】。

测试直接量到了这一点【测试】(`tests/test_filters.py:304-321`)：输入 `1/30`，经 `bwdif(send_field:tff:all)` 后输出 `1/60`，且第 1 个输入帧产出 0 帧、其后每帧产出 2 帧、`pts` 为 `(frame.pts-1)*2` 与 `+1`：

```python
assert filtered_frames[0].time_base == Fraction(1, 60)
assert filtered_frames[0].pts == (frame.pts - 1) * 2
```

结论：**输出 `pts` 落在滤镜决定的时基里，与输入 `pts` 不是同一单位**，使用方在比较/拼接前必须自行换算（该测试用 `1/30 → 1/60` 的倍数关系做的是显式换算，而非依赖绑定层）。

---

## 2. 单路结束与全部结束：调用层能否分别表达

### 2.1 结论先说

| 需求 | 可表达？ | 手段 | 缺口 |
| --- | --- | --- | --- |
| 单路结束 | **是** | `graph.push(None, at=i)` | `at` 使用"全源"索引空间，与帧推送的索引空间不同（见 2.2） |
| 全部输入结束 | **是** | `graph.push(None)`（广播 `buffer`+`abuffer`） | 混合图会**同时**结束音频与视频 |
| 结束全部视频源（保留音频） | 是 | `graph.vpush(None)` | 无对应的"只结束全部音频源"方法 |
| 结束全部音频源（保留视频） | 否（无专用 API） | 逐源 `push(None, at=i)` | 需自己枚举 `abuffer` 的下标（且受累加索引影响，见 2.2） |
| 查询某源是否已结束 | **否** | — | 无状态查询属性 |
| 查询某汇是否还会产出 | **否** | — | 只能靠 `pull()` 抛 EAGAIN/EOF |
| 多汇分别拉取 | 是，但须绕开 `Graph.pull` | `FilterContext.pull()` | `Graph.pull()` 要求单汇 |

所以"单路 / 全部"在**调用形态**上可以分别表达，但"局部结束"不是一个被建模的状态，只是"对一个源写一次 NULL"这个动作；绑定层不记录、不可查询。

### 2.2 `at` 的索引空间在 EOS 与帧推送时不同（真实陷阱）

`Graph.push` 选集合的规则【代码】(`av/filter/graph.py:283-294`)：

```python
if frame is None:
    contexts = self._get_context_by_type("buffer") + self._get_context_by_type("abuffer")
elif isinstance(frame, VideoFrame):
    contexts = self._get_context_by_type("buffer")
elif isinstance(frame, AudioFrame):
    contexts = self._get_context_by_type("abuffer")
```

`contexts[at]` 随后用它做下标【代码】(`av/filter/graph.py:296-305`)。于是在"1 个视频源 + 1 个音频源"的图里：

- `vpush(frame, at=1)` → `IndexError`（视频列表只有 1 项）；
- `push(None, at=1)` → 命中 **`abuffer`**（EOS 时索引空间是 video→audio 的拼接）。

同一次 `at=1`，在传帧与传 `None` 时的目标不同。`vpush(None, at=i)` 又只按视频列表索引【代码】(`av/filter/graph.py:307-319`），于是 EOS 有**两套**索引语义。仓库测试只在纯视频双输入图上用了 `at`（两个源都是 `buffer`）【测试】(`tests/test_filters.py:374-390`)，因此这个不一致没有被触发，也没有测试覆盖。

实用契约：**多类型图里结束某一路时，用 `vpush(None, at=i)` / `push(None, at=i)` 前先确认你要的索引空间；或者干脆只对单一媒体类型图使用 `at`。**

### 2.3 `FilterContext.push(None)` 绕过了委托分支（UB 风险）

`FilterContext.push` 的分支顺序【代码】(`av/filter/context.py:110-122`)：

```python
if self._kind == _KIND_SOURCE or frame is None:
    self.graph.configure()
if frame is None:
    res = lib.av_buffersrc_write_frame(self.ptr, cython.NULL)   # <-- 对任意上下文都写 NULL
    err_check(res); return
elif self._kind == _KIND_SOURCE:
    res = lib.av_buffersrc_write_frame(self.ptr, frame.ptr)
    err_check(res); return
# 只有"非 None 且非源"才走到下面的委托
```

即：对**任何**非源上下文调用 `push(None)`，会把该滤镜的 `AVFilterContext*` 当成 `AVBufferSrcContext*` 传给 `av_buffersrc_write_frame()`。这在 Cython 层是类型混用（AGENTS.md 的 "look out for UB" 正指此类）。它同时也解释了为什么"单路结束"在 `Graph.push` 上是安全的：`Graph.push(None)` 只遍历 `buffer`/`abuffer` 上下文。

仓库测试从不用 `FilterContext.push(None)`；EOS 一律走 `graph.push(None)` / `vpush(None)`【测试】(`tests/test_filters.py:282`, `346`, `389-390`)。所以**这条路径目前无证据支持，契约上应避开**。

### 2.4 汇结束 ≠ 全部输入结束

"汇返回 EOF"是**单个汇**的终态，与"所有源都收到 None"不是一回事【待验证，判据 V4】。帧同步类滤镜（`overlay` 一类）通常把"主输入结束"当作输出结束的信号，即使次输入还有帧；此时源仍可继续写入但不会产出【待验证，判据 V6】——这条在仓库内没有任何测试或源码依据，只能由 V4/V6 实测确认。绑定层对此没有任何表达或拦截：`push` 只检查索引越界【代码】(`av/filter/graph.py:296-302`），不检查该源是否已 EOF、也不检查所在图是否已无汇可拉。

---

## 3. 暂时无输出与最终结束如何区分

### 3.1 唯一的区分手段是异常类型

`pull()` 把 `av_buffersink_get_frame()` 的返回值直接交给 `err_check()`【代码】(`av/filter/context.py:154-156`)。`err_check` 只有两类结果【代码】(`av/error.py:327-354`)：`res >= 0` 正常返回；`res < 0` 按 `-res` 查 `classes` 抛出。与本议题相关的两条：

- `EAGAIN` → `av.BlockingIOError`（`_extend_builtin("BlockingIOError", (errno.EAGAIN, ...))`）【代码】(`av/error.py:214-217`)。
- `AVERROR_EOF` → `av.EOFError`（`("EOF", -lib.AVERROR_EOF, "EOFError", EOFError)`）【代码】(`av/error.py:147`)；由 `av/error.py:265-282` 的类型组装生成，基类固定为 `(FFmpegError, EOFError)`，所以它同时可被 `FFmpegError` 与内建 `EOFError` 捕获（`tests/test_errors.py:28-35` 验证了同一种"FFmpeg 错误 + 内建异常"双继承模式）。

`err_check` 还有一个前置动作：**先检查 Cython 回调里暂存的 Python 异常**（`av/error.py:319-326`，`stash_exception()` 在 `293-308`）。所以若滤镜内部触发了 PyAV 回调错误，抛出的是原始 Python 异常，而不是 EAGAIN/EOF —— 这两条才会被错误地归入"暂无输出"的类里。

### 3.2 两种异常的语义边界

| 返回值 | PyAV 异常 | 调用层可读出的语义 | 不可读出的内容 |
| --- | --- | --- | --- |
| `EAGAIN` | `av.BlockingIOError` | 此刻没有可取的帧 | 需要多少输入、当前是否已有源结束、之后是否还会产出 |
| `AVERROR_EOF` | `av.EOFError` | 该汇不会再给出帧 | 是否所有源都已结束、是哪个滤镜决定了结束 |

测试给出的两个直接证据：

- **没有任何输入时 pull 得到 EAGAIN**【测试】(`tests/test_filters.py:160-176`)：
  ```python
  audio_buffer.link_to(graph.add("abuffersink"))
  graph.configure()
  try:
      graph.pull()
  except OSError as e:
      # we haven't pushed any input so expect no frames / EAGAIN
      if e.errno != errno.EAGAIN: raise
  ```
  这说明 "EAGAIN" 同时覆盖"尚未喂数据"。
- **某些滤镜会一直扣住输出直到 EOS，此时不推 None 就只有 EAGAIN**【测试】(`tests/test_filters.py:344-349`)：
  ```python
  graph.vpush(None)
  # if we do not push None, we get a BlockingIOError
  palette_frame = graph.vpull()
  ```
  `palettegen` 只在 EOS 时产出调色板帧。也就是说 EAGAIN 有时表示"该汇要等到 EOS 才产出"，与"输入不足"在异常类型上无法区分。

### 3.3 测试里已经成型的"拉取到阻塞为止"约定

`tests/test_filters.py:39-47` 的辅助函数是仓库内对 EAGAIN 语义的正式表态【测试】：

```python
def pull_until_blocked(graph: Graph) -> list[av.VideoFrame]:
    while True:
        try:
            frames.append(graph.vpull())
        except av.FFmpegError as e:
            if e.errno != errno.EAGAIN: raise
            return frames
```

它**只把 EAGAIN 当作"暂时没有"并返回，把其它错误（含 EOF）继续抛出**。因此该辅助函数只适用于"还没有结束"的阶段；一旦推入 `None`，继续用它就会把 EOF 当异常抛给调用者。`_test_video_buffer` 正是每帧 `vpush` + `pull_until_blocked`（`tests/test_filters.py:304-321`），全程不 EOS。

---

## 4. 局部结束 + 内部延迟：三类可观测行为

按仓库测试能分辨的形态，滤镜分为三类。

### 4.1 立即产出（无延迟）

`volume`（`tests/test_filters.py:230-257`）、`aformat` 重采样（`178-202`）、`lutrgb`（`64-87`）：推一帧即可拉一帧。EOS 后立刻 EOF，不会多出帧。

### 4.2 固定内部延迟：首帧被扣住，稳态 1→N

`bwdif(send_field)`【测试】(`tests/test_filters.py:308-321`）：**第一个输入帧产出 0 帧**（注释 `# no output for the first input frame`），之后每帧产出 2 帧。这是"局部已有输入、尚无输出"的最干净的证据，也说明 EAGAIN 在稳态中途是**正常**的（`pull_until_blocked` 每帧都会返回）。

未在测试里推 `None`，所以**尾部那 1 帧是否补出属于【待验证】**（判据 V2）。

### 4.3 仅在 EOS 产出（EOS-gated）

- `palettegen`【测试】(`tests/test_filters.py:331-353`）：不推 `None` 只有 EAGAIN；推了 `None` 之后一次 `vpull()` 拿到 16×16 的调色板帧。
- `ebur128(metadata=1)`【测试】(`tests/test_filters.py:259-291`）：先 `graph.push(frame)` 再 `graph.push(None)`，然后 `graph.pull()` 才拿到带 `lavfi.r128.I` / `lavfi.r128.LRA` 的帧。**若省略 `push(None)`，这一帧永远不会出现。**

这两条正是"一路输入结束并不等于输出结束"的正向证据：输出的**存在**依赖 EOS 信号本身。

### 4.4 音频延迟：flush 是否产出取决于具体滤镜/参数

`AudioResampler` 就是在图上做 `abuffer → [aresample] → aformat → abuffersink`【代码】(`av/audio/resampler.py:93-128`），其 `resample(None)` 是仓库里唯一完整的"EOS + 排空"实现【代码】(`av/audio/resampler.py:138-151`)：

```python
self.graph.push(frame)
output = []
while True:
    try:
        output.append(self.graph.pull())
    except EOFError:
        break
    except FFmpegError as e:
        if e.errno != EAGAIN: raise
        break
```

测试量化了尾部帧的**存在性与长度都不固定**【测试】：

- 48000→44100（降采样），flush 得到 1 帧 15 samples，pts 接续 `941 + 925`【测试】(`tests/test_audioresampler.py:155-162`)；
- 44100→48000，flush 得到 1 帧 18 samples【测试】(`tests/test_audioresampler.py:198-205`)；
- 采样率不变、只做 `s16→fltp` 格式转换但设置 `frame_size=1024` 的用例，flush 得到 **0 帧**【测试】(`tests/test_audioresampler.py:208-242`)；
- 输入帧未设 `time_base`、48000→44100 的 `test_pts_missing_time_base`，flush 得到 1 帧 16 samples【测试】(`tests/test_audioresampler.py:245-269`)。

结论：**"flush 后拿不到帧"是合法结果（延迟已被前面的帧吸收或恰好整除），不能据此判定"尾帧丢了"。** 区分"本就无尾帧"与"尾帧被丢弃"只能靠与滤镜声明的延迟/采样数账目对齐（判据 V2）。

### 4.5 仓库内的反例：示例代码会丢 atempo 的尾帧

`examples/audio/atempo.py:11-29`：

```python
graph.link_nodes(graph.add_abuffer(template=input_stream),
                 graph.add("atempo", "2.0"),
                 graph.add("abuffersink")).configure()
for frame in input_file.decode(input_stream):
    graph.push(frame)
    while True:
        try:
            for packet in output_stream.encode(graph.pull()): output_file.mux(packet)
        except (av.BlockingIOError, av.EOFError):
            break
# Flush the stream
for packet in output_stream.encode(None): output_file.mux(packet)
```

两处问题：

1. **从不 `graph.push(None)`**：`atempo` 是带有内部延迟的音频滤镜（本仓库无源码，须按【待验证，判据 V2】实测其声明延迟），不 EOS 就无从判断尾段是否已经吐出；至少"推完即停"没有任何机制保证尾段被取到；
2. 即使加了 EOS，内层 `except (av.BlockingIOError, av.EOFError): break` 也会**在第一次 EAGAIN 就退出**，而不是继续拉到 EOF——EOS 刚推入时汇很可能先给 EAGAIN 再给帧或 EOF（判据 V3）。

最后只 `encode(None)` 刷**编码器**，对滤镜图无效。这是"提前结束 + 尾帧丢失"的完整范式错误。

---

## 5. 剩余输入的时间戳如何决定后续帧的位置

三条彼此独立的机制，只有第一条是绑定层决定的：

1. **链接时间基（绑定层只读）**：输出 `pts` 的单位取自汇输入链接【代码】(`av/filter/context.py:159`)。不同输入源可以声明不同时间基（`add_buffer`/`add_abuffer` 各自独立），协商发生在 `avfilter_graph_config()`（`Graph.configure()`，`av/filter/graph.py:94`）。绑定层**不参与协商**，也不把结果回写到调用者的帧上。
2. **帧同步的选择（滤镜决定）**：多输入合成滤镜（`overlay` 等）在一路结束时，是"重复最后一帧继续"、"终止输出"还是"以最短者为准"，由滤镜自己的选项（帧同步类滤镜常见的 `eof_action`、`shortest`、`repeatlast`，确切名称/取值以本机 `ffmpeg -h filter=<名字>` 输出为准）决定【待验证，判据 V4/V5】。绑定层只是把选项字符串交给 `avfilter_init_dict`/`avfilter_init_str`（`av/filter/context.py:73-91`），**无法覆盖这些策略**。
3. **重打时间戳（滤镜决定）**：`aresample`/`aformat` 重采样按样本数重算输出 `pts`，输出 `pts` 与输入 `pts` 不是线性对应；测试里 1/1000 时基的输入 `pts=8192` 对应输出 `pts=65536`（1/8000 时基）【测试】(`tests/test_audioresampler.py:228-238`）。`atempo` 一类变速滤镜是否同样重排 `pts`，仓库无测试，按【待验证，判据 V2】处理。另外任何滤镜都可能产出 `pts is None`：帧的 `Frame.pts` 对 `AV_NOPTS_VALUE` 返回 `None`【代码】(`av/frame.py:115-117`），`Frame.time` 同样返回 `None`（`166-168`）。

由此得到"剩余输入的时点如何决定后续帧位置"的可操作答案：

- 输出帧在**时间轴**上的位置 = 汇链接时间基 × 输出 `pts`；该 `pts` 由滤镜推进，绑定层没有"剩余输入最后一帧 pts + 帧长"这样的推算逻辑（`pull()` 之外没有任何时间计算）。
- 对帧同步类合成，**未结束的输入继续向滤镜供帧**才是"后续帧"的来源；已结束输入如何参与由该滤镜的结束策略选项决定（重复最后一帧 / 立即结束 / 其它）【待验证，判据 V4/V5】。
- 对音频合成，输入会先按链接协商后的时间基/采样率进入同一路径，再按样本计数前进【待验证，判据 V7】；因此"剩余输入的时间戳"只影响**它自己那条支路**，输出的推进由汇侧样本计数决定。仓库能直接证实的只有单输入 `aresample` 的情形【测试】(`tests/test_audioresampler.py:122-269`)。

**下游一致性**：`CodecContext.encode()` 会把帧 rebase 到编码器时基【代码】(`av/codec/context.py:706-713`)，但 `Frame._rebase_time()` 在 `pts == AV_NOPTS_VALUE` 时**跳过重标定**，仅设置时基【代码】(`av/frame.py:65-84`)：

```python
if self.ptr.pts != lib.AV_NOPTS_VALUE:
    self.ptr.pts = lib.av_rescale_q(self.ptr.pts, self._time_base, dst)
```

所以滤镜产出的"无 pts 帧"会以无时间戳进入编码/封装，位置由下游猜测。契约上必须在喂编码器前确认 `out.pts is not None`。

---

## 6. 调用契约

### 6.1 完成（flush）的总则

```python
# 每个独立的输入生产者，各自结束
graph.push(None, at=i)          # 或 vpush(None, at=i)，注意索引空间，见 2.2

# 全部生产者结束后，排空
while True:
    try:
        frame = sink.pull()      # 多汇时用 FilterContext.pull()
    except av.EOFError:
        break                    # 该汇终态：尾帧已排空
    except av.BlockingIOError:
        continue                 # 暂时没有：不要 break！(见 4.5)，但要加自旋上限，见下
    else:
        use(frame)
```

`continue` 需要一个防护：在**所有源都已 EOS**之后，汇若持续返回 `EAGAIN`，上面就是死循环。契约写成"EAGAIN 计数超过阈值即视为该实现不保证排空并报错退出"，而不是无条件重试。反过来，在**尚未 EOS** 的阶段，EAGAIN 是完全正常的（见 4.1-4.3），不需要防护。

要点与理由：

1. **EOS 与排空必须分开建模**。"推 None"只告诉某个源"没更多了"，不保证任何输出；要有输出必须继续 `pull()`。证据：`palettegen`/`ebur128` 的产物只在 EOS 后出现【测试】(`tests/test_filters.py:259-291`, `331-353`）。
2. **`EAGAIN` 绝不能当成结束**。`bwdif` 首帧 0 输出【测试】(`tests/test_filters.py:308-315`）、`AudioResampler` 每次 `resample(frame)` 都以 EAGAIN/EOF 结束拉取循环、且每个输入帧只给一帧输出【测试】(`tests/test_audioresampler.py:74-118` 与 `av/audio/resampler.py:141-149`），都说明 EAGAIN 是常态。EOS 之后同样可能先 EAGAIN（判据 V3）。
3. **`EOFError` 是唯一合法的终止信号**，且它只对**被拉取的那一个汇**成立（判据 V4）。
4. **不要对同一源重复 `push(None)`**。预期 FFmpeg 的 buffersrc 对已 EOF 的源返回 `AVERROR_EOF`，经 `err_check` 会变成 `av.EOFError`【代码】(`av/filter/context.py:113-117` → `av/error.py:327-354`）——本仓库无此测试，列入【待验证，判据 V1】。无论结论如何，契约上写成"每个源恰好一次"。
5. **`push(None)` 之后不要再向该源推帧**。绑定层不拦截，行为取决于 FFmpeg【待验证，判据 V1】。

### 6.2 多输入合成

- 只结束**已经真的没有数据**的那一路；不要为了"让它跑起来"提前 `push(None)` 到仍在产出的支路。提前 EOS 会让 framesync 按滤镜策略收尾（重复/终止），等价于截断【待验证，判据 V5】。
- 需要"最短者决定"或"继续重复最后一帧"时，**只能靠滤镜参数**（`overlay` 一类帧同步滤镜的结束策略选项、`apad` 补尾等；确切参数名与默认值以本机 `ffmpeg -h filter=<名字>` 为准）；绑定层没有开关可调【代码】(`av/filter/context.py:73-91` 只做参数透传）。
- 只要有一个汇已经 EOF，就不要再把它的源当成"还有意义"；继续推是静默丢弃【待验证，判据 V6】。

### 6.3 带延迟的音频

- 输入结束后**必须**排空到 `EOFError`（`AudioResampler.resample(None)` 是仓库内的正确模板【代码】(`av/audio/resampler.py:138-151`)）。
- 不要用 `set_audio_frame_size()` 来表达"尾部补齐"：它只设置 `abuffersink` 的出帧粒度【代码】(`av/filter/graph.py:258-271`），要求图已配置且有 `abuffersink`，不改变尾帧是否存在【测试】(`tests/test_audioresampler.py:208-242` 的 0 帧尾）。
- 需要固定总长时，用显式补尾滤镜（如 `apad`），而不是"多推几个空帧"。
- 输出 `samples`/`pts` 的账目应可闭合：`sum(out.samples)` × 时基 ≈ 输入时长 + 声明的延迟【判据 V2】。

### 6.4 与编码器/封装交接

- 在 `encode()` 之前检查 `out.pts is not None`；`_rebase_time` 不会为无 pts 帧造时间戳【代码】(`av/frame.py:65-84`)。
- 注意输出时基与输入时基不同（`bwdif` 的 1/30→1/60【测试】(`tests/test_filters.py:317-321`)），若要与输入帧对齐（例如按原始视频帧计数），需自行按比值换算，不要假定同单位。
- 多汇图（视频+音频）分别用 `FilterContext.pull()`；`Graph.pull()` 会因 `nsinks != 1` 抛 `ValueError`【代码】(`av/filter/graph.py:326-330`)。

---

## 7. 规则归属：绑定层 vs 具体滤镜

### 绑定层决定（在本仓库可查到，且对任何滤镜一致）

| 事项 | 位置 |
| --- | --- |
| 源的识别（只认 `buffer`/`abuffer`） | `av/filter/context.py:28-37` |
| 每个源的时间基由调用者声明，缺省猜测 1/1000（含弃用告警）/ 1/sample_rate | `av/filter/graph.py:194-207`, `243-249` |
| push 不做时间基换算、不做一致性校验 | `av/filter/context.py:113-122` |
| EOS 是对单个源上下文写 NULL | `av/filter/context.py:113-117` |
| `push(None)` 广播到所有 `buffer`+`abuffer`；`at` 在该拼接列表上索引 | `av/filter/graph.py:283-305` |
| `push(frame, at=i)` 只在同类型列表上索引 | `av/filter/graph.py:287-302`, `307-319` |
| `FilterContext.push(None)` 不对非源做委托（UB 风险） | `av/filter/context.py:110-122` |
| EAGAIN → `av.BlockingIOError`；EOF → `av.EOFError`；回调异常优先 | `av/error.py:214-217`, `147`, `319-326` |
| 输出帧时间基取自汇的输入链接 | `av/filter/context.py:159` |
| `Graph.pull`/`vpull` 要求单汇 | `av/filter/graph.py:322-339` |
| `configure()` 惰性、配置后加入的滤镜在下一次 push/pull 时配置 | `av/filter/graph.py:89-98`, `136-144` |
| 滤镜参数原样透传（不解释结束策略类选项的名称与取值） | `av/filter/context.py:73-91` |

补充：`configure(auto_buffer=True, force=False)` 的 `auto_buffer` 形参在函数体内**未被使用**【代码】(`av/filter/graph.py:90-98`），即绑定层并未提供任何"自动插入缓冲/FIFO"的开关；跨时间基/跨帧长的自动插桩完全由 FFmpeg 配置阶段决定【待验证】。

### 取决于具体 FFmpeg 滤镜（绑定层无法承诺）

| 事项 | 影响 |
| --- | --- |
| 某一输入 EOS 后是否继续产出（帧同步滤镜的 `eof_action` 一类选项，取值随滤镜而异） | 决定"局部结束"是否等于"输出结束" |
| 是否存在"以最短输入为准"的选项及其默认值 | 决定最早结束的输入是否截断输出 |
| 内部延迟大小、EOS 时补出多少尾帧 | 决定"flush 得到 0 帧"是正常还是丢尾 |
| 输出链接时间基与 `pts` 重定时 | 决定输出 `pts` 的单位与步长 |
| 对非单调/缺失 pts 的处理（报错或补齐） | 决定 `pull()` 是否抛 `InvalidDataError`/`ArgumentError` |
| 多输入音频的混音时长策略 | 决定最终长度与结束点 |

---

## 8. 当前证据无法保证的输出时序：验证判据

以下每条都可写成独立 pytest（用 `lavfi` 生成器或合成帧，不需要外部媒体服务），且不依赖网络。

**V1 — 重复 EOS / EOS 后推帧的行为**
对同一 `abuffer` 连续两次 `push(None)`，第二次应当抛出 `av.EOFError`（且 `err_check` 未把它翻译成别的东西）；对已 EOS 的源 `push(frame)` 应报错而非静默接受。
判据：`pytest.raises(av.EOFError)`（或记录实际返回的 errno，若为 0 则说明 FFmpeg 静默接受，契约需改为"调用者自行保证一次"）。
覆盖缺口：`tests/test_filters.py` 全部 EOS 用例都只推一次。

**V2 — 尾帧完整性（延迟滤镜）**
以 `bwdif`、`atempo`、`aresample`、`apad` 各建单输入图，记录 `sum(in.nb_samples|frame_count)`、滤镜声明延迟、`sum(out.samples|frame_count)`。
判据：`out_total == in_total + declared_delay`（在换算到同一时基后）；对 `bwdif` 断言"全量输入 + EOS 后总帧数 = 2×(输入帧数−1)+尾部补齐量"，且最后一个输出 `pts` 不小于最后输入 `pts`。
已知基线：`tests/test_filters.py:308-321` 已给出无 EOS 时的 0/2 帧节奏；`tests/test_audioresampler.py:155-162`, `198-205`, `208-242`, `245-269` 给出四种 flush 结果（1 帧 15 / 1 帧 18 / 0 帧 / 1 帧 16）。
风险点：`bwdif` 无 EOS 测试；`examples/audio/atempo.py` 明确不 EOS。

**V3 — EOS 后的 EAGAIN/EOF 顺序**
推 `None` 后连续 `pull()`：期望序列为 0..N 次 EAGAIN、若干帧、一次 EOFError，且此后恒为 EOFError。
判据：断言"首次 EOFError 之后不再出现帧"；断言"EOS 之后至少出现过一次 EAGAIN 或直接出帧"，从而证明 `except (BlockingIOError, EOFError): break` 这种写法会丢尾。
覆盖缺口：`AudioResampler.resample()` 的排空循环对 EAGAIN 也是 `break`【代码】(`av/audio/resampler.py:141-149`)，与 EOF 同等对待。现有四个 flush 用例（`tests/test_audioresampler.py:155-162`, `198-205`, `208-242`, `245-269`）里 EOS 后都直接给帧或直接给 EOF，所以从未暴露问题；若某个 FFmpeg 版本/参数组合在 EOS 后先给一次 EAGAIN，`resample(None)` 会**返回空列表并静默丢掉尾帧**。这正是 V3 要固定的行为。

**V4 — 汇终态 ≠ 输入全结束**
`overlay` 双输入：先 `push(None, at=0)`（主输入），保持 `at=1` 继续推帧，观察 `vpull()`。
判据：若 `at=1` 仍有帧被接受但 `vpull()` 返回 EOFError，则"输出结束"发生在主输入结束处，与次输入是否结束无关；需记录该滤镜默认的结束策略选项取值。反向用例：先结束 `at=1`（次输入），断言输出是否继续（策略为"重复最后一帧"时应继续，为"立即结束/跳过"时应结束）。选项名与合法取值先用 `ffmpeg -h filter=overlay` 确认，再写进测试。
覆盖缺口：`tests/test_filters.py:355-397` 只测"两路都推 None 后能拿到一帧"，不测中间状态。

**V5 — 滤镜策略选项确实生效**
按本机 `ffmpeg -h filter=overlay` 列出的结束策略选项，各取一组取值分别建图（例如"以最短者为准"开/关、次输入结束动作的三种取值），固定输入长度（例如视频 10 帧、叠加源 5 帧）。
判据：输出帧数随选项变化（10 / 5 / 提前结束三种之一）；同时断言输出 `pts` 严格单调递增、落在汇链接时基。
目的：证明"局部结束如何收尾"只由滤镜参数决定，绑定层无权干预。

**V6 — 汇 EOF 后继续推源**
在一个汇已 EOF 的图上继续 `push` 到尚未 EOS 的源一次，然后再次 `pull()`。
判据：`push` 不抛错、`pull` 稳定抛 `EOFError`（即"静默丢弃"成立）；若 `push` 抛错，则调用层需新增"已结束"状态检查。
用途：判定 `push` 是否需要（或已经隐含）状态校验。

**V7 — 跨时间基多输入的时间位置**
两个 `buffer` 源分别声明 `1/30`（视频）与 `1/1000`（时间基准），`overlay` 合成。
判据：`out.time_base` 报告的是汇链接协商出的时基，且与两个输入声明的时间基中至少一个不同；`out.pts` 序列在换算到该时基后步长恒定，等于视频源的帧间隔。
目的：验证 1.3 的推论——PyAV 绑定不做换算，伪时间基错误会静默错位。
覆盖缺口：`tests/test_filters.py:375-378` 两源同为 `1/30`，无跨时基用例。

**V8 — 声明时间基与实际帧 pts 不一致时的可观测性**
用 `add_buffer()` 的缺省（`1/1000`，会触发 `DeprecationWarning`，`av/filter/graph.py:195-200`）建图，却按 `1/30` 推送 `pts`。
判据：断言"不抛任何异常"，且输出 `time`（`Frame.time`，`av/frame.py:156-168`）与真实时间存在 33.3 倍偏差。若观察到异常，说明 FFmpeg 侧有校验，契约可放宽。
用途：把"静默错位"从推论升级为结论。

**V9 — 多汇图的分别排空**
建"视频 `buffersink` + 音频 `abuffersink`"图：`Graph.pull()` 应抛 `ValueError`（`av/filter/graph.py:326-330`）；`sink_v.pull()`/`sink_a.pull()` 各自独立返回 EOF。
判据：EOS 后两个汇的 EOF 时刻可以不同；断言分别拉取不会互相阻塞。
覆盖缺口：全仓测试没有任何多汇图用例（`av/filter/graph.py:321` 的 TODO）。

**V10 — 无 pts 输出的下游位置**
用会产出 `pts is None` 的情形（例如构造缺 pts 的输入帧，或使用重打时间戳的滤镜），先 `pull()` 再 `encode()`。
判据：`out.pts is None` 时 `Frame.time is None`（`av/frame.py:166-168`），且 `CodecContext.encode()` 不注入时间戳（`av/frame.py:76-77` 的跳过分支）。据此在契约中要求"喂编码器前显式补/校验 pts"。

---

## 9. 一句话总结

**"某一路输入结束"在 PyAV 里只是"对某个 `buffer`/`abuffer` 写一次 NULL"：可以单路表达（`push(None, at=i)`），也可以全体表达（`push(None)`），但"局部结束"不被建模、不可查询，而且 `at` 的索引空间在帧与 EOS 之间并不一致；"暂时无输出"与"最终结束"只能靠 `av.BlockingIOError` 与 `av.EOFError` 区分，前者在合成与延迟滤镜下是常态，后者只对单个汇成立。输出帧的位置由汇链接的时基与滤镜推进的 pts 决定，绑定层既不换算也不校验，所以完成时的正确姿势是：逐源、恰好一次地推 `None`，然后**持续拉到 `EOFError` 为止**，把 `EAGAIN` 一律当作"继续"。哪些"结束即收尾、重复最后一帧、以最短者截断"的规则生效，全在具体 FFmpeg 滤镜及其参数；绑定层能保证的只有异常类型与索引/广播形态。**
