# Remux 的 Extradata 契约

无重编码转封装（remux）时，输出容器能否解释码流由两件事共同决定：

1. **模板流参数** —— 建流时从输入流复制到 `codecpar` 的内容；
2. **首批包的 extradata** —— 文件头写出（`avformat_write_header`）之前，能否拿到码流的参数集（parameter sets）。

本文梳理本仓库（PyAV）的相关实现，区分**可直接复制的参数**、**必须处理的格式转换**，并定义**封装成功 / 可播放 / 语义完整**三种结果层级。

---

## 1. 仓库实现总览

### 1.1 三条建流路径（`av/container/output.py`）

| 路径 | 位置 | CodecContext | extradata 来源 | 适用场景 |
|---|---|---|---|---|
| `add_stream()` | `output.py:140` | 新建编码器 ctx | 编码器打开后生成 | 转码 |
| `add_stream_from_template()` | `output.py:362` | 新建 ctx，从模板 codecpar 回填 | 模板 `codecpar.extradata` 经 ctx 中转复制 | remux（有 cctx 的模板） |
| `add_mux_stream()` | `output.py:275` | **无** | 无 —— 依赖首包提取（见 §1.3） | 外部已编码的包直接进封装 |
| `_add_stream_without_codec_from_template()` | `output.py:444` | 无 | `avcodec_parameters_copy` 整体复制 | attachment / data 流 |

`add_stream_from_template` 的关键步骤（`output.py:407-428`）：

```python
err_check(lib.avcodec_parameters_to_context(ctx, template.ptr.codecpar))
ctx.codec_tag = 0                          # 假设在 remux，重置容器相关 tag
stream.time_base = template.ptr.time_base  # AVCodecParameters 不带 time_base，必须显式复制
ctx.time_base = template.ptr.time_base
if self.ptr.oformat.flags & lib.AVFMT_GLOBALHEADER:
    ctx.flags |= lib.AV_CODEC_FLAG_GLOBAL_HEADER
if not opaque:
    ctx.flags = template.cctx.flags        # 有意覆盖上面的 GLOBAL_HEADER
err_check(lib.avcodec_parameters_from_context(stream.codecpar, ctx))
```

- `avcodec_parameters_to_context` 把模板 codecpar（含 `extradata`）灌进新 ctx，再由 `avcodec_parameters_from_context` 写回输出流的 codecpar —— **extradata 的复制以"模板 codecpar 里有"为前提**。
- 该 ctx 被标记 `_template_initialized`（`output.py:436`，`_ctxflags |= 1`），`start_encoding()` 因此**不会打开编码器**（`output.py:619`），纯 remux 不发生编解码。

### 1.2 文件头写出的时机

`start_encoding()`（`output.py:601`）在**首个包进入 `_mux_one` 时**惰性触发：先对每条流做 `_finalize_for_output()`（`av/stream.py:177`，把 cctx 参数最终刷进 codecpar、复制 metadata），再 `avformat_write_header`。

> `add_stream` 的 docstring 明确警告：**在 mux 第一个包之前配置好所有输出流**；头写出后 format、宽高、layout、rate 等结构性属性不可再改。这也意味着 codecpar 的 extradata 必须在头写出前就绪。

### 1.3 首包 extradata 提取（`_buffer_for_extradata`，`output.py:800-881`）

这是"首批包的 extradata"决定输出可解释性的核心机制：

1. **触发条件**（仅在第一个包到来时规划一次，`_myflag & 16`）：
   - 输出格式带 `AVFMT_GLOBALHEADER` 标志（MP4、Matroska 等要求参数集与包分离存放的容器）；
   - 流**没有** cctx **且** codecpar 上**没有** extradata —— 即 `add_mux_stream` 建出来的裸流。
2. 为每条这样的流挂一个 `extract_extradata` 比特流过滤器（`av/bitstream.py` 的 `BitStreamFilterContext`），**缓冲所有包**，暂停头的写出。
3. 每个流的首包到达时（`_try_extract_extradata`，`output.py:838`）：克隆包送入 bsf，从输出包的 `AV_PKT_DATA_NEW_EXTRADATA` side data 中取出参数集，经 `_set_codecpar_extradata`（`output.py:19`，`av_malloc` + padding + memcpy）写入 `codecpar.extradata`。
4. **首包是找带内参数集的唯一可靠位置**——无论提取成功与否，该流都只尝试这一次，之后回落到 muxer 默认行为（`output.py:878-881`）。
5. 所有流的 extradata 就绪（或放弃）后，一次性写出文件头并冲刷缓冲包；`close_output` 也会兜底冲刷（`output.py:72-77`）。

回归测试：`test_add_mux_stream_matroska_extradata`（`tests/test_remux.py:92`，issue #2198）—— annex-b 裸 H.264 包进 Matroska，muxer 需要 `CodecPrivate`，靠首包提取才能写头成功且可解码。

### 1.4 包路径

`mux_one`（`output.py:764`）→ `_buffer_for_extradata`（可能缓冲）→ `_mux_one`（`output.py:771`）：校验 `stream_index` → `packet._rebase_time(stream.time_base)`（`av/packet.py:289`，按输出流 time_base 重缩放 pts/dts/duration）→ `av_interleaved_write_frame`。

### 1.5 反向转换：比特流过滤器

`BitStreamFilterContext`（`av/bitstream.py`）支持显式转换，典型如 `h264_mp4toannexb`（AVCC → Annex-B，进 MPEG-TS 等容器时必需）。注意：

- 需要 extradata 才能工作的过滤器（如 `h264_mp4toannexb`）**必须以完整 Stream 作 `in_stream`**，只给编解码器名无法正确转换（`tests/test_bitstream.py:77-84`）；
- 传 `out_stream` 会把过滤器的输出参数（含改写后的 extradata）刷回 codecpar 和 cctx（`bitstream.py:58-69`）。

包级 side data 类型 `new_extradata` 已暴露在 `av/packet.py:21`，但 PyAV 只在写头前消费它（§1.3）；**头写出后码流中段的参数集变更不会回流到 codecpar**。

---

## 2. 可直接复制的参数

以下参数经 `avcodec_parameters_to_context` / `avcodec_parameters_copy` 或显式赋值即可安全搬运，无需转换：

| 参数 | 复制路径 | 备注 |
|---|---|---|
| `codec_id` / `codec_type` | codecpar 复制 | 前提：输出格式支持，见 §3.1 |
| 宽高、像素格式、profile/level | codecpar 复制 | |
| 采样率、声道布局、采样格式 | codecpar 复制 | |
| `bit_rate` 等码率字段 | codecpar 复制 | 仅信息性 |
| **extradata（若模板 codecpar 中已存在）** | codecpar ↔ ctx 往返 | MP4→MKV 等同为 length-prefixed 的容器间直接成立 |
| `time_base` | **显式复制**（`output.py:413-414`） | `AVCodecParameters` 不携带 time_base，漏复制则输出流 time_base 为 None（`test_add_stream_from_template_copies_time_base`） |
| `avg_frame_rate` | 显式赋值 | |
| disposition、metadata、start_time、duration | `_add_stream_without_codec_from_template`（`output.py:462-470`） | 仅无 cctx 的附件/数据流路径复制 |
| 附件 payload | 附件流的 extradata 即 payload（`output.py:496-507`、`av/stream.py:400`） | |

---

## 3. 必须处理的格式转换

以下各项**不能**照搬，照搬即产出坏文件：

### 3.1 容器能力校验

`avformat_query_codec(..., FF_COMPLIANCE_NORMAL)`（`output.py:177, 316, 391`）：编解码器不被目标容器支持时直接 `ValueError`。这是转换的第一道闸。

### 3.2 codec_tag 重置

`ctx.codec_tag = 0`（`output.py:410`）：tag 是源容器的 fourcc，搬进目标容器可能无意义甚至冲突，必须清零让 muxer 自选。

### 3.3 Annex-B ↔ length-prefixed（extradata 形态转换）

H.264/H.265 的参数集有两种存在方式，容器各认一种：

- **源是 Annex-B（带内参数集），目标是 GLOBALHEADER 容器（MP4/MKV）**：模板 codecpar 里没有 extradata。两条路：
  - `add_mux_stream` + 自动首包提取（§1.3，仅 GLOBALHEADER 输出、无 cctx 的流）；
  - 手动 `extract_extradata` bsf 或编码时开 `GLOBAL_HEADER`。
  - 注意：此时**包本身仍是 Annex-B**，进 MP4 还需 `h264_mp4toannexb` 的逆转换——PyAV 不自动做包体转换，需用户挂 bsf。
- **源是 AVCC（extradata 在 codecpar），目标是 Annex-B 容器（MPEG-TS）**：必须显式挂 `h264_mp4toannexb`（`tests/test_bitstream.py:61`），且 `in_stream` 要给完整 Stream 让过滤器读到 extradata。

### 3.4 GLOBAL_HEADER 标志的所有权

`add_stream_from_template` 先按输出格式置 `AV_CODEC_FLAG_GLOBAL_HEADER`（`output.py:417-418`），随后非 opaque 时又被 `ctx.flags = template.cctx.flags` **有意覆盖**（`output.py:422-423`）——模板的 flags 优先。remux 时这是对的（不重编码，flags 只影响参数解释），但若混用编码路径需意识到覆盖顺序。

### 3.5 时间戳语义

- **flushing 包 vs 无 DTS 关键帧**：demux 末尾会产出 `size == 0` 的冲刷包，必须跳过；但**不能**用 `dts is None` 过滤——MKV 只存 PTS，B 帧流头部重排包（含关键帧）demux 出来 DTS 就是 None，误删关键帧则输出不可解码（issue #1917，`test_remux_keeps_keyframe_with_none_dts`，`examples/basics/remux.py:17-22`）。
- **time_base 重缩放**：包进 `_mux_one` 时按输出流 time_base 重缩放（`packet.py:289`），所以输出流 time_base 必须先对（§2）。
- **initial_padding（AAC priming samples）**：模板复制会带上源流的 `initial_padding`（MKV 的 CodecDelay）。整流 remux 没问题；**从中间切一段**时必须 `out_stream.cctx.initial_padding = 0` 并把 pts/dts 平移到 0 起点，否则解码器会丢掉段首 1024 个真实采样（`test_remux_clear_initial_padding`，`tests/test_remux.py:236`）。

### 3.6 中段参数变更

`new_extradata` side data（`av/packet.py:21`）表示码流中途参数集更新。PyAV 的自动提取只覆盖**首包**（写头前）；头写出后 codecpar 冻结，中段变更不会反映到输出流参数——依赖此特性的码流 remux 后语义可能漂移。

---

## 4. 三种结果层级

### 4.1 封装成功（muxing success）

`avformat_write_header` 与 `av_interleaved_write_frame` 均返回 0，文件结构写完。必要条件：

- 编解码器被目标容器接受（§3.1）；
- muxer 写头所需的 codecpar 齐备 —— 对 GLOBALHEADER 容器即 **extradata 在写头前就绪**（模板复制或 §1.3 首包提取，否则如 #2198 直接写头失败）；
- 包的 `stream_index` 合法（`output.py:776-780`）；
- 时间戳能被 muxer 接受（`av_interleaved_write_frame` 内部校验）。

**此层级不保证任何解码器能读。**

### 4.2 可播放（playable）

文件能被解码出帧/采样。在 4.1 之上要求：

- **extradata 形态与包体形态匹配容器**：Annex-B 包进了只认 AVCC 的容器（或反之），头能写出但解码器拿不到/误读参数集（§3.3）；
- 首个关键帧未被误删（§3.5 的 `size == 0` 判别）；
- 参数集确实可得：extradata 或可靠的带内参数集至少其一；`extract_extradata` 对非 Annex-B 输入会失败并静默回落（`output.py:862-864`），此时若带内也没有参数集，输出不可解码；
- time_base 正确，时间戳不塌缩。

验证方式即仓库测试的做法：重新打开输出并 `decode` 计帧（`_decoded_frame_count`，`tests/test_remux.py:184`）。

### 4.3 语义完整（semantically complete）

播放结果与源**逐帧等价且元数据无损**。在 4.2 之上要求：

- 时间语义完整：切割场景清 `initial_padding` 并平移时间戳（§3.5）；时长、起始时间正确；
- 全部流类型搬迁：附件/数据流走 `_add_stream_without_codec_from_template`，disposition、metadata 一并复制（§2）；
- B 帧重排信息完整（dts/pts 关系未被过滤逻辑破坏）；
- 包级 side data（display matrix、HDR 元数据、`new_extradata` 中段更新等）按需保留——PyAV 默认透传包体与 side data，但 §3.6 的参数漂移不在此保证内；
- codec_tag 与目标容器惯例一致（§3.2）。

---

## 5. 速查：写头前 extradata 决策

```
输出格式是 GLOBALHEADER（MP4/MKV）？
├─ 否（如 MPEG-TS）→ 参数集须带内；源是 AVCC 则挂 h264_mp4toannexb（§3.3）
└─ 是 → codecpar.extradata 必须在 avformat_write_header 前就绪
   ├─ add_stream_from_template：模板 codecpar 有 extradata → 自动复制（§1.1）
   │   模板没有（源是 Annex-B 裸流）→ 需自行提取或改用 add_mux_stream
   ├─ add_mux_stream：无 cctx → 自动缓冲首包 + extract_extradata（§1.3）
   └─ add_stream（转码）：编码器开 GLOBAL_HEADER 后自产 extradata
```
