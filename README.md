# obt — OpenBatchTranscoding

Encoding sniffing and batch transcoding. **Explainable verdicts, verified writes, no silent corruption.**

编码嗅探与批量转码。**判定可解释，转码有校验，绝不静默损坏文件。**

> Every paragraph below is given in English first, then in Chinese.
> 下文每段话都是上面英文、下面中文。
>
> CLI output is currently Chinese-only; it is quoted verbatim in the samples below.
> 命令行提示目前只有中文，下面的示例输出是原样引用。

```
$ obt sniff legacy/ --fail-on non-utf8
  嗅探 12 个文件

  File                    Encoding    Conf  BOM  EOL   Size    Note
  ----------------------  ----------  ----  ---  ----  ------  --------------------------
  legacy/Program.cs       gbk         0.82  无   CRLF  12.4 KB
  legacy/Report.cs        gbk         0.79  无   CRLF  8.1 KB
  legacy/mixed.log        utf-8       0.30  无   —     4.1 KB  疑似混合编码：7 字节无法解释
```

---

## Why build a CLI first, and not a toolbox / 为什么先做 CLI，不做工具箱

DevToys-style "developer toolboxes" are genuinely pleasant to use, but they are the *result* of an ecosystem that already existed — the shell was put on top of it afterwards. Projects that go the other way around (shell first, engine later) usually die at the second feature: the shell fills up with logic, there is no second caller, no script can reuse any of it, and it ends up as a small tool you can only click through by hand.

DevToys 那种"开发工具箱"用着是香，但它是**先长出了 CLI 生态，才套上那层壳**。反过来的项目——先做壳、再补内核——通常死在第二件事上：壳里塞满了逻辑，没有第二个调用方，也没有脚本能复用它，最后变成一个只能手动点的小工具。

So this project does it the other way around: **the detection engine is built to be callable on its own, and the CLI is merely its first shell.**

所以这里反过来：**先把判定引擎做成可独立调用的东西，CLI 只是它的第一个外壳。**

```
src/obt/
├── core/            ← the engine. Pure logic + file IO, no printing, no color
│   ├── encodings.py   codec registry, BOM table, alias normalization
│   ├── heuristics.py  four-component scoring (struct / quality / script / prior)
│   ├── detect.py      orchestrates the verdict, produces a Detection with evidence
│   ├── decode.py      strict decoding, structured errors
│   ├── mixed.py       locate mixed encodings, per-line encoding distribution
│   ├── eol.py         line-ending detection and conversion (orthogonal to encoding)
│   ├── scan.py        directory walking and ignore rules
│   ├── plan.py        the transcode plan (work it all out before touching disk)
│   └── apply.py       atomic writes, pre-replacement self-verification
├── cli.py           ← the first shell: argparse + one handler per command
└── render.py        ← terminal rendering: wide-char alignment, color switch
```

There are two ways to attach a shell, and neither requires touching the engine:

有两种"接壳"方式，都不需要动内核：

```python
# 1. call the library directly / 直接调库
from obt.core import detect, build_plan, apply_actions
det = detect(open("a.cs", "rb").read())
print(det.encoding, det.confidence, det.evidence)

# 2. consume JSON (every command supports --json; the contract is below)
#    吃 JSON（所有命令都支持 --json，契约见下文）
```

A GUI, a web service, an MCP tool, an editor plugin — all of them get added on top of this layer later, rather than reaching into the engine to change how detection works. **The JSON output is the API that those shells will call.**

将来要加 GUI、Web 服务、MCP 工具、编辑器插件，都是在这一层之上加，而不是进去改判定逻辑。**JSON 输出就是那层"壳"要调用的 API。**

---

## Install and run / 安装与运行

Zero dependencies — Python standard library only (`gbk / gb18030 / big5 / shift_jis / euc_jp / euc_kr / cp1252` are all built-in codecs, so no network access and no packages to install):

零依赖，只用 Python 标准库（`gbk / gb18030 / big5 / shift_jis / euc_jp / euc_kr / cp1252` 都是 Python 自带的 codec，不需要联网、不需要装包）：

```bash
# run without installing / 免安装直接跑
PYTHONPATH=src python -m obt sniff .

# or install it as a command / 或者装成命令
pip install -e .          # afterwards just use `obt` / 之后直接用 obt
```

Requires Python ≥ 3.9.

需要 Python ≥ 3.9。

---

## The four commands / 四个命令

### `sniff` — what encoding is this file, and on what grounds / 这份文件到底是什么编码，凭什么

```bash
obt sniff src/legacy.cs              # one-line verdict / 一行结论
obt sniff src/legacy.cs --explain    # print the full scoring table / 打出候选编码的打分明细表
obt sniff logs/ --per-line           # per-line encoding mix / 逐行编码分布，定位混合编码
obt sniff src/ --fail-on non-utf8    # exit 1 on a hit, CI-ready / 命中就退出码 1，可直接进 CI
```

`--explain` is where the real value of this tool lives. Instead of handing you an opaque verdict, it lays out all four scores for every candidate codec:

`--explain` 是这个工具的核心价值所在。它不给你一个黑箱结论，而是把每个候选编码的四项得分摊开：

```
  候选打分（总分 = 0.35×结构 + 0.35×质量 + 0.30×文字系统 + 先验 + 决胜）
  编码          严格解码  结构  质量  文字   先验   决胜   总分   解码失败原因
  ------------  --------  ----  ----  ----  -----  ----  ------  ----------------------
  utf-8           失败    1.00  0.08  1.00  +0.15     -       -  偏移 0：invalid start byte
  gbk              OK     0.99  1.00  1.00  +0.00     -    0.997
  gb18030          OK     0.99  1.00  1.00  -0.05     -    0.947
  euc-kr           OK     1.00  1.00  0.44  -0.02     -    0.812
  windows-1252     OK     0.45  1.00  0.15  -0.20     -    0.352
```

### `scan` — take the measure of a tree: what is actually mixed in here / 摸一遍底：仓库里都混着些什么

```bash
obt scan . --include '*.cs'
obt scan . --fail-on non-utf8          # CI gate: red if anything is non-UTF-8 / CI 门禁：有非 UTF-8 就红
obt scan . --min-confidence 0.6        # list the "not sure" files separately / 把"没把握"的文件单独列出来
```

### `convert` — batch transcode: plan first, write second / 批量转码，先出计划再落地

```bash
# step 1: see what would change (touches nothing) / 第一步：看一眼要改什么（不碰磁盘）
obt convert . --to utf-8-bom --eol lf --dry-run

# step 2: once you are happy, execute / 第二步：确认无误再执行
obt convert . --to utf-8-bom --eol lf --yes
obt convert . --to utf-8-bom --yes --backup       # also keep .bak / 顺便留 .bak
obt convert . --to gbk --out-dir out --yes        # mirror into a new dir instead / 不原地改，镜像到新目录
```

**Writing to disk requires an explicit `--yes`; there is no interactive confirmation.** This is not laziness: on Windows, `sys.stdin.isatty()` can return `True` even for `DEVNULL`, and guessing wrong about "am I interactive?" costs you a file that has already been overwritten. Better to make you type one extra flag.

**写盘必须显式加 `--yes`，没有交互式确认。** 这不是偷懒：Windows 上连 `DEVNULL` 的 `sys.stdin.isatty()` 都可能返回 `True`，"靠终端状态猜是否交互"猜错的代价是直接写盘。宁可要求多打一个参数。

`--eol` defaults to `keep`: **change the encoding only, leave line endings alone.** Ask for `--eol lf` explicitly if you also want them normalized. Coupling those two together is a common cause of explosively large repo diffs.

`--eol` 默认是 `keep`：**只改编码，不动换行符**。想顺手统一换行才显式写 `--eol lf`。这两件事耦合在一起是仓库 diff 爆炸的常见原因。

### `check` — the CI gate / CI 门禁

```bash
obt check . --expect utf-8-bom --eol lf
```

Writing `utf-8-bom` also demands a BOM; writing `utf-8` looks at the encoding only and ignores the BOM.

写成 `utf-8-bom` 会连带要求 BOM；写 `utf-8` 则只看编码、不管 BOM。

### `encodings` — supported encodings and aliases / 支持的编码与别名

---

## How the verdict is reached / 判定是怎么做的

Four components, all explainable, all reproducible, and none of them needing a word-frequency table or training data:

四个分量，全部可解释、可复现、不需要词频表或训练数据：

| Component / 分量 | Weight / 权重 | What it looks at / 看什么 |
|---|---|---|
| `struct` structure<br>结构 | 0.35 | byte grammar; whether two-byte sequences land in the codec's *most common* ranges<br>字节语法 + 双字节序列是否落在该编码**最常用**的区间 |
| `quality` quality<br>质量 | 0.35 | share of control chars / U+FFFD / private-use chars in the decoded result<br>解码结果里控制符 / U+FFFD / 私用区的占比 |
| `script` writing system<br>文字系统 | 0.30 | whether it decodes to Simplified, Traditional, kana, hangul or Western letters<br>解出来的是简体、繁体、假名、谚文还是西欧字母 |
| `prior` prior<br>先验 | ±0.35 | UTF-8 gets a bonus as the modern default; latin-1 decodes any bytes at all, so it is the weakest evidence and gets a penalty<br>UTF-8 是现代默认给正分；latin-1 能解任意字节，证据最弱给负分 |
| `decisive` decisive bonus<br>决胜 | +0.10 | a large amount of hangul / kana — evidence that was not guessed into existence<br>解出大量谚文 / 假名这种"不是猜出来的"证据 |

A few criteria that actually do the work deserve their own explanation:

几条真正管用的判据，值得单独说明：

**The GB2312 level-1 hanzi range.** The 3,755 most common Chinese characters sit in `0xB0A1–0xD7F9`; level-2 characters sit in `0xD8A1–0xF7FE`; anything starting `0x81–0xA0` is a GBK extension (rare characters). The overwhelming majority of two-byte pairs in real Chinese text land in level 1. So when UTF-8 bytes are misread as GBK, the pile of rare characters it decodes to drags the structure score down.

**GBK 的一级汉字区。** GB2312 一级汉字（最常用 3755 字）落在 `0xB0A1–0xD7F9`，二级字在 `0xD8A1–0xF7FE`，`0x81–0xA0` 开头的是 GBK 扩展（生僻字）。真实中文文本的双字节绝大头落在一级区。所以"UTF-8 字节被当成 GBK 读"这种错判，解出来的一堆生僻字会把结构分拉下来。

**Whether two-byte pairs "borrowed" an ASCII byte.** Real Chinese text has hanzi running in runs, so high bytes naturally pair up with each other. When Western text is force-read as hanzi, every high byte borrows the ASCII letter after it as its trail byte, so the ratio approaches 100%. `0xB0 0x43` (`°C`) is syntactically valid GBK, but the adjacency ratio gives it away.

**双字节对是不是"借"了 ASCII 字节。** 真中文文本汉字连片，高位字节自然成对；而"西欧文本被硬当汉字读"时，每个高位字节都在借用它后面的 ASCII 字母当尾字节，比例接近 100%。`0xB0 0x43`（`°C`）这种在 GBK 里语法合法，但邻接比例会暴露它。

**Korean and Simplified Chinese overlap in byte space.** The KS X 1001 hangul range (`B0A1–C8FE`) and the GB2312 level-1 hanzi range (`B0A1–D7F9`) sit on the same bytes, so a Simplified-Chinese GBK file can often be "decoded" by EUC-KR — but what comes out is a *mixture* of hangul and hanzi, whereas genuine Korean text is overwhelmingly hangul with hanzi as occasional garnish. That ratio is the only way to tell the two apart.

**韩文与简体中文的字节空间是重叠的。** KS X 1001 的谚文区（`B0A1–C8FE`）和 GB2312 一级汉字区（`B0A1–D7F9`）压在同一片字节上，所以一份简体中文 GBK 文件往往能被 EUC-KR"解出来"——但解出来是谚文与汉字的**混杂**，而真正的韩文文本里谚文占绝对多数、汉字只是点缀。这条判据是分开这两者的唯一办法。

**The mojibake fingerprint.** When UTF-8 Chinese is read as cp1252, each 3-byte character becomes three letters from the `À–ÿ` range, which can exceed 30% of the text; genuine German or French text has a few percent of accented letters at most. Above 12%, the tool declares "this is not Western text, this is something else that has been read as Western text" and **refuses** to give an encoding verdict.

**乱码指纹。** UTF-8 的中文被当 cp1252 读时，每个 3 字节字变成 3 个 `À-ÿ` 区字母，占比能到三成以上；真正德语/法语文本里变音字母撑死几个百分点。超过 12% 就判为"这不是西欧文本，是别的东西被读成了西欧文本"，并**拒绝**给出编码结论。

---

## The refusal rules: better to do nothing than to do it wrong / 拒绝规则：宁可不动，不可动错

All four of the following are **refused**, reported as `status=error`, and never quietly waved through:

下面四种情况一律**拒绝转码**，写成 `status=error`，绝不静默放过：

1. **Determined to be non-text** — it contains NUL bytes, or its share of control bytes is too high.
   **判定为非文本** —— 含 NUL、或控制字节占比过高
2. **Cannot be strictly decoded** — mixed encodings or corruption. Force-converting with `errors='replace'` turns still-rescuable bytes into permanent U+FFFD, which is the most unforgivable way for an encoding tool to fail: it does not error out, but the result is wrong. The right move is to locate the problem with `--per-line` first, then handle it in segments with `--from`.
   **无法严格解码** —— 混合编码 / 损坏。用 `errors='replace'` 硬转会把还能救的字节永久变成 U+FFFD，这是编码工具最不可原谅的失败方式：它不报错，但结果不对。正确做法是先 `--per-line` 定位，用 `--from` 分段处理
3. **Confidence below the threshold** (0.6 by default) — a wrong guess means damaged content, so a human decides.
   **置信度低于阈值**（默认 0.6）—— 猜错就是内容损毁，让人来定
4. **The target encoding cannot represent some of the characters** (emoji into GBK) — the tool names the exact character for you.
   **目标编码表示不了其中某些字符**（emoji 转 GBK）—— 直接告诉你是哪个字

The order of operations when writing is deliberate too:

落地时的顺序也是刻意设计的：

```
写 *.obt-tmp → fsync → 读回来验字节 / 验能不能解回 / 验字符数 → 通过才 os.replace
```

Verification happens *before* the replacement, so when verification fails the original file is completely untouched and there is nothing to roll back afterwards (rollback can fail too). `os.replace` is atomic: a power loss mid-write leaves either the original file or the complete new one, never a half-written source file.

校验发生在**替换之前**，所以"校验不过"时原文件毫发无损，不需要事后回滚（回滚本身也可能失败）。`os.replace` 是原子的，中途断电要么是原文件、要么是完整的新文件，不会留下写了一半的源码。

---

## Exit codes / 退出码

| Code / 码 | Meaning<br>含义 |
|---|---|
| 0 | Everything is fine<br>一切正常 |
| 1 | Some file does not meet expectations (a `--fail-on` hit / non-conforming / a file was refused)<br>有文件不符合预期（命中 `--fail-on` / 不合规 / 有文件被拒绝转码） |
| 2 | Usage error or invalid path<br>用法错误或路径无效 |

---

## Output encoding: pipes get real UTF-8 / 输出编码：管道里给真 UTF-8

`obt` looks at where its output is going before it prints anything:

- **Piped or redirected** (`| jq`, `> report.txt`, CI logs): output is forced to **UTF-8**, whatever the console code page happens to be. JSON is the machine-facing contract, and replacing Chinese file names or evidence with `?` just to please a legacy code page would be silent data corruption.
- **Interactive console**: keeps the console's own encoding (cp936 on a Chinese Windows, so Chinese still displays), and only relaxes `errors` to `replace`. A character the code page cannot represent degrades to `?` — it never raises `UnicodeEncodeError` and kills the command.

This rule came out of a real failure: on GitHub Actions `windows-latest` stdout is cp1252, so printing Chinese crashed with exit code 1 — which collided with the exit code this tool uses to mean "a file does not meet expectations".

`obt` 在打印之前会先看输出去哪，据此决定编码策略：

- **管道 / 重定向**（`| jq`、`> report.txt`、CI 日志）：强制输出 **UTF-8**，不管控制台代码页是什么。因为 JSON 是给机器消费的契约——为了迁就老代码页把中文文件名与判定证据换成 `?`，等于静默损坏数据。
- **交互式控制台**：保留控制台自己的编码（中文 Windows 上是 cp936，中文照常显示），只把 `errors` 放宽成 `replace`。代码页装不下的字符退化成 `?`，但绝不抛 `UnicodeEncodeError` 把整条命令带崩。

这条规则是踩出来的：GitHub Actions 的 `windows-latest` 上 stdout 是 cp1252，打印中文直接崩、退出码 1——而退出码 1 恰好是本工具用来表示"有文件不符合预期"的信号，两者撞在一起极难排查。

---

## The JSON contract / JSON 契约

Every command supports `--json` and emits a stable structure for programs to consume:

所有命令都支持 `--json`，输出结构稳定，供程序消费：

```json
{
  "tool": "obt", "version": "0.1.0", "command": "sniff",
  "summary": { "files": 12, "violations": 2, "fail_on": ["non-utf8"] },
  "items": [{
    "path": "legacy/Program.cs",
    "encoding": "gbk", "label": "gbk", "confidence": 0.82,
    "bom": "", "eol": "crlf", "size": 12698,
    "strict_decodable": true, "ambiguous": false,
    "evidence": ["所有候选编码中总分最高", "打分：结构 0.99 / …"],
    "alternatives": [{ "encoding": "gb18030", "total": 0.947 }]
  }]
}
```

The per-candidate `candidates` detail is only included with `sniff --explain`; `convert --json` carries the full `plan` and `results`. Detail is omitted by default so that the JSON does not explode on a repository with thousands of files.

`sniff --explain` 时才会带上逐候选的 `candidates` 明细；`convert --json` 会带完整的 `plan` 与 `results`。默认不带明细，是为了在几千个文件的仓库上 JSON 不会爆炸。

---

## Known limits: stated plainly, no claims of omnipotence / 已知边界：说清楚，不装全能

- **UTF-16 with no BOM and no NUL bytes at all is not detectable.** The criterion is "all NUL bytes must sit on the same parity", and a pure-CJK UTF-16 file contains no NUL at all. In practice UTF-16 files always contain newlines, spaces or ASCII digits, so this limit is rarely hit; if you do hit it, pass `--from utf-16le`.
  **完全没有 NUL 且无 BOM 的 UTF-16 认不出来。** 判据是"NUL 必须落在同一奇偶位"，而纯 CJK 的 UTF-16 文件不含 NUL。现实中 UTF-16 文件总有换行/空格/ASCII 数字，所以这条限制基本不会碰到；真碰到了用 `--from utf-16le`。
- **A verdict is a probability, not a fact.** A score gap below 0.08 is explicitly flagged as "structurally ambiguous" with a recommendation to spot-check. For a sample of a single hanzi in two bytes, the tool still gives a verdict but pushes confidence below 0.65 and marks it ambiguous — it does not pretend to know.
  **判定给的是概率，不是事实。** 分差小于 0.08 会明确标注"存在结构性歧义"，并建议抽样核对。样本只有一个汉字两个字节时，工具会给出结论但把置信度压到 0.65 以下并标注歧义——不假装自己知道。
- **`struct` is a heuristic, not a standard.** It is designed around the range facts of GB2312 / Big5 / JIS and is effective for those codecs; for an obscure codec that is not covered, it degrades to "everything else failed".
  **`struct` 是启发式，不是标准。** 它按 GB2312 / Big5 / JIS 的分区事实设计，对这些编码有效；对没收录的冷门编码只能退化成"别的都不成立"。
- **It does not read `.gitignore`.** Ignore rules are a built-in directory-name allowlist plus `--include/--exclude`. To follow `.gitignore`, feed it `git ls-files`.
  **不读 `.gitignore`。** 忽略规则是内置的目录名白名单 + `--include/--exclude`。要按 `.gitignore` 走，用 `git ls-files` 喂给它。

---

## Development and tests / 开发与测试

```bash
python -m unittest discover -s tests -t .        # 158 cases, stdlib only, no install needed
                                                 # 158 个用例，纯标准库，无需安装
python scripts/make_samples.py                   # build samples/ and self-check it
                                                 # 生成 samples/ 编码试验场并自检
```

`tests/fixtures.py` holds 13 samples that are *deliberately saved in different encodings*, each corresponding to a class of real-world incident (a legacy GBK `.cs`, a log mixing UTF-8 and GBK, BOM-less UTF-16, GB18030 with four-byte sequences, …). The script and the tests share one corpus, so running `make_samples.py` prints an "expected vs actual" comparison table.

`tests/fixtures.py` 里是 13 份"刻意用不同编码保存"的样本，每一份都对应一类真实事故（历史遗留 GBK 的 .cs、混着 UTF-8 与 GBK 的日志、无 BOM 的 UTF-16、含四字节序列的 GB18030……）。脚本与测试共用同一份语料，跑一遍 `make_samples.py` 就能看到"预期 vs 实际"的对照表。

When a one-line verdict is not enough and you want every piece of evidence at once:

自我感觉不够、需要一次性看全部依据时：

```bash
obt sniff samples/ --explain --per-line
```

---

## This repository's own encoding rules / 本仓库的编码约定

A tool about encoding uniformity has to practice it itself: **source files are UTF-8 (no BOM) + LF.**

一个关于编码统一的工具，自身必须以身作则：**源码统一 UTF-8（无 BOM）+ LF**。

The rules live in two places, `.editorconfig` (editor layer) and `.gitattributes` (version-control layer). **Both are required**: with `.editorconfig` alone, Windows' default `core.autocrlf=true` converts working-tree files back to CRLF at checkout, at which point "the tool does not even satisfy its own `--eol lf` gate". The `* text=auto eol=lf` line in `.gitattributes` is the layer that overrides `autocrlf` and guarantees LF in both the index and the working tree.

约定写在 `.editorconfig`（编辑器层）和 `.gitattributes`（版本控制层）两处。**两个文件缺一不可**：只有 `.editorconfig` 时，Windows 上默认的 `core.autocrlf=true` 会在 checkout 时把工作区文件换回 CRLF，于是"工具自己都不符合自己的 `--eol lf` 门禁"。`.gitattributes` 里的 `* text=auto eol=lf` 是覆盖 `autocrlf` 的那一层，它保证索引和工作区都存 LF。

Point the tool at itself and everything should pass:

把本工具指向自己，应该全部通过：

```bash
PYTHONPATH=src python -m obt check . --expect utf-8 --eol lf
```

The only thing that errors when scanning the whole repo with the command above is `samples/` — a test bed deliberately filled with GBK / Big5 / Shift_JIS / CP1252 bytes, which is "non-conforming" by design and is excluded in `.gitignore`. To check tracked files only:

上面这条扫描整个仓库时，唯一会报错的是 `samples/`——那是刻意装满 GBK / Big5 / Shift_JIS / CP1252 字节的试验场，本来就"不合规"，且已在 `.gitignore` 中排除。只查受控文件：

```bash
PYTHONPATH=src python -m obt check src tests scripts .github \
  README.md pyproject.toml uv.lock \
  .editorconfig .gitattributes .gitignore \
  --expect utf-8 --eol lf
```

That command is the CI self-bootstrap gate (see `.github/workflows/ci.yml`): the tool's own source, tests, scripts, the workflow itself and all config files must be judged UTF-8 + LF by the tool itself. If any of them fail, `check` exits 1.

这条命令就是 CI 的自举门禁（见 `.github/workflows/ci.yml`）：工具源码、测试、脚本、workflow 自身，以及所有配置文件，都必须由本工具自己判定为 UTF-8 + LF。判定不符时 `check` 以退出码 1 失败。

You can also check the other way round, asking git what line endings it actually stored:

也可以反过来核对 git 到底把行尾存成了什么：

```bash
git ls-files --eol        # expect all i/lf  w/lf / 期望全部是 i/lf  w/lf
```
