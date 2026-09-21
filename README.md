# obt — OpenBatchTranscoding

编码嗅探与批量转码。**判定可解释，转码有校验，绝不静默损坏文件。**

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

## 为什么先做 CLI，不做工具箱

DevToys 那种"开发工具箱"用着是香，但它是**先长出了 CLI 生态，才套上那层壳**。
反过来的项目——先做壳、再补内核——通常死在第二件事上：壳里塞满了逻辑，
没有第二个调用方，也没有脚本能复用它，最后变成一个只能手动点的小工具。

所以这里反过来：**先把判定引擎做成可独立调用的东西，CLI 只是它的第一个外壳。**

```
src/obt/
├── core/            ← 引擎。纯逻辑 + 文件 IO，没有任何打印与颜色
│   ├── encodings.py   编码注册表、BOM 表、别名归一
│   ├── heuristics.py  四分量打分（结构 / 质量 / 文字系统 / 先验）
│   ├── detect.py      编排判定，产出带证据的 Detection
│   ├── decode.py      严格解码、结构化报错
│   ├── mixed.py       混合编码定位、逐行编码分布
│   ├── eol.py         行尾检测与转换（与编码正交）
│   ├── scan.py        目录遍历与忽略规则
│   ├── plan.py        转码计划（改前先算清楚）
│   └── apply.py       原子写、替换前自校验
├── cli.py           ← 第一个外壳：argparse + 一个 handler 一个命令
└── render.py        ← 终端渲染：宽字符对齐、颜色开关
```

两种"接壳"方式，都不需要动内核：

```python
# 1. 直接调库
from obt.core import detect, build_plan, apply_actions
det = detect(open("a.cs", "rb").read())
print(det.encoding, det.confidence, det.evidence)

# 2. 吃 JSON（所有命令都支持 --json，契约见下文）
```

将来要加 GUI、Web 服务、MCP 工具、编辑器插件，都是在这一层之上加，
而不是进去改判定逻辑。**JSON 输出就是那层"壳"要调用的 API。**

---

## 安装与运行

零依赖，只用 Python 标准库（`gbk / gb18030 / big5 / shift_jis / euc_jp / euc_kr / cp1252`
都是 Python 自带的 codec，不需要联网、不需要装包）：

```bash
# 免安装直接跑
PYTHONPATH=src python -m obt sniff .

# 或者装成命令
pip install -e .          # 之后直接用 obt
```

需要 Python ≥ 3.9。

---

## 四个命令

### `sniff` — 这份文件到底是什么编码，凭什么

```bash
obt sniff src/legacy.cs              # 一行结论
obt sniff src/legacy.cs --explain    # 打出候选编码的打分明细表
obt sniff logs/ --per-line           # 逐行编码分布，定位混合编码
obt sniff src/ --fail-on non-utf8    # 命中就退出码 1，可直接进 CI
```

`--explain` 是这个工具的核心价值所在。它不给你一个黑箱结论，而是把
每个候选编码的四项得分摊开：

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

### `scan` — 摸一遍底：仓库里都混着些什么

```bash
obt scan . --include '*.cs'
obt scan . --fail-on non-utf8          # CI 门禁：有非 UTF-8 就红
obt scan . --min-confidence 0.6        # 把"没把握"的文件单独列出来
```

### `convert` — 批量转码，先出计划再落地

```bash
# 第一步：看一眼要改什么（不碰磁盘）
obt convert . --to utf-8-bom --eol lf --dry-run

# 第二步：确认无误再执行
obt convert . --to utf-8-bom --eol lf --yes
obt convert . --to utf-8-bom --yes --backup       # 顺便留 .bak
obt convert . --to gbk --out-dir out --yes        # 不原地改，镜像到新目录
```

**写盘必须显式加 `--yes`，没有交互式确认。** 这不是偷懒：Windows 上连
`DEVNULL` 的 `sys.stdin.isatty()` 都可能返回 `True`，"靠终端状态猜是否交互"
猜错的代价是直接写盘。宁可要求多打一个参数。

`--eol` 默认是 `keep`：**只改编码，不动换行符**。想顺手统一换行才显式写
`--eol lf`。这两件事耦合在一起是仓库 diff 爆炸的常见原因。

### `check` — CI 门禁

```bash
obt check . --expect utf-8-bom --eol lf
```

写成 `utf-8-bom` 会连带要求 BOM；写 `utf-8` 则只看编码、不管 BOM。

### `encodings` — 支持的编码与别名

---

## 判定是怎么做的

四个分量，全部可解释、可复现、不需要词频表或训练数据：

| 分量 | 权重 | 看什么 |
|---|---|---|
| `struct` 结构 | 0.35 | 字节语法 + 双字节序列是否落在该编码**最常用**的区间 |
| `quality` 质量 | 0.35 | 解码结果里控制符 / U+FFFD / 私用区的占比 |
| `script` 文字系统 | 0.30 | 解出来的是简体、繁体、假名、谚文还是西欧字母 |
| `prior` 先验 | ±0.35 | UTF-8 是现代默认给正分；latin-1 能解任意字节，证据最弱给负分 |
| `decisive` 决胜 | +0.10 | 解出大量谚文 / 假名这种"不是猜出来的"证据 |

几条真正管用的判据，值得单独说明：

**GBK 的一级汉字区。** GB2312 一级汉字（最常用 3755 字）落在 `0xB0A1–0xD7F9`，
二级字在 `0xD8A1–0xF7FE`，`0x81–0xA0` 开头的是 GBK 扩展（生僻字）。
真实中文文本的双字节绝大头落在一级区。所以"UTF-8 字节被当成 GBK 读"这种错判，
解出来的一堆生僻字会把结构分拉下来。

**双字节对是不是"借"了 ASCII 字节。** 真中文文本汉字连片，高位字节自然成对；
而"西欧文本被硬当汉字读"时，每个高位字节都在借用它后面的 ASCII 字母当尾字节，
比例接近 100%。`0xB0 0x43`（`°C`）这种在 GBK 里语法合法，但邻接比例会暴露它。

**韩文与简体中文的字节空间是重叠的。** KS X 1001 的谚文区（`B0A1–C8FE`）
和 GB2312 一级汉字区（`B0A1–D7F9`）压在同一片字节上，所以一份简体中文 GBK 文件
往往能被 EUC-KR"解出来"——但解出来是谚文与汉字的**混杂**，而真正的韩文文本里
谚文占绝对多数、汉字只是点缀。这条判据是分开这两者的唯一办法。

**乱码指纹。** UTF-8 的中文被当 cp1252 读时，每个 3 字节字变成 3 个 `À-ÿ` 区字母，
占比能到三成以上；真正德语/法语文本里变音字母撑死几个百分点。超过 12% 就判为
"这不是西欧文本，是别的东西被读成了西欧文本"，并**拒绝**给出编码结论。

---

## 拒绝规则：宁可不动，不可动错

下面四种情况一律**拒绝转码**，写成 `status=error`，绝不静默放过：

1. **判定为非文本** —— 含 NUL、或控制字节占比过高
2. **无法严格解码** —— 混合编码 / 损坏。用 `errors='replace'` 硬转会把还能救的
   字节永久变成 U+FFFD，这是编码工具最不可原谅的失败方式：它不报错，但结果不对。
   正确做法是先 `--per-line` 定位，用 `--from` 分段处理
3. **置信度低于阈值**（默认 0.6）—— 猜错就是内容损毁，让人来定
4. **目标编码表示不了其中某些字符**（emoji 转 GBK）—— 直接告诉你是哪个字

落地时的顺序也是刻意设计的：

```
写 *.obt-tmp → fsync → 读回来验字节 / 验能不能解回 / 验字符数 → 通过才 os.replace
```

校验发生在**替换之前**，所以"校验不过"时原文件毫发无损，不需要事后回滚
（回滚本身也可能失败）。`os.replace` 是原子的，中途断电要么是原文件、
要么是完整的新文件，不会留下写了一半的源码。

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 一切正常 |
| 1 | 有文件不符合预期（命中 `--fail-on` / 不合规 / 有文件被拒绝转码） |
| 2 | 用法错误或路径无效 |

---

## JSON 契约

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

`sniff --explain` 时才会带上逐候选的 `candidates` 明细；`convert --json`
会带完整的 `plan` 与 `results`。默认不带明细，是为了在几千个文件的仓库上
JSON 不会爆炸。

---

## 已知边界（说清楚，不装全能）

- **完全没有 NUL 且无 BOM 的 UTF-16 认不出来。** 判据是"NUL 必须落在同一奇偶位"，
  而纯 CJK 的 UTF-16 文件不含 NUL。现实中 UTF-16 文件总有换行/空格/ASCII 数字，
  所以这条限制基本不会碰到；真碰到了用 `--from utf-16le`。
- **判定给的是概率，不是事实。** 分差小于 0.08 会明确标注"存在结构性歧义"，
  并建议抽样核对。样本只有一个汉字两个字节时，工具会给出结论但把置信度压到
  0.65 以下并标注歧义——不假装自己知道。
- **`struct` 是启发式，不是标准。** 它按 GB2312 / Big5 / JIS 的分区事实设计，
  对这些编码有效；对没收录的冷门编码只能退化成"别的都不成立"。
- **不读 `.gitignore`。** 忽略规则是内置的目录名白名单 + `--include/--exclude`。
  要按 `.gitignore` 走，用 `git ls-files` 喂给它。

---

## 开发与测试

```bash
python -m unittest discover -s tests -t .        # 158 个用例，纯标准库，无需安装
python scripts/make_samples.py                   # 生成 samples/ 编码试验场并自检
```

`tests/fixtures.py` 里是 13 份"刻意用不同编码保存"的样本，
每一份都对应一类真实事故（历史遗留 GBK 的 .cs、混着 UTF-8 与 GBK 的日志、
无 BOM 的 UTF-16、含四字节序列的 GB18030……）。脚本与测试共用同一份语料，
跑一遍 `make_samples.py` 就能看到"预期 vs 实际"的对照表。

自我感觉不够、需要一次性看全部依据时：

```bash
obt sniff samples/ --explain --per-line
```

---

## 本仓库的编码约定

一个关于编码统一的工具，自身必须以身作则：**源码统一 UTF-8（无 BOM）+ LF**。

约定写在 `.editorconfig`（编辑器层）和 `.gitattributes`（版本控制层）两处。
**两个文件缺一不可**：只有 `.editorconfig` 时，Windows 上默认的
`core.autocrlf=true` 会在 checkout 时把工作区文件换回 CRLF，
于是"工具自己都不符合自己的 `--eol lf` 门禁"。`.gitattributes` 里的
`* text=auto eol=lf` 是覆盖 `autocrlf` 的那一层，它保证索引和工作区都存 LF。

把本工具指向自己，应该全部通过：

```bash
PYTHONPATH=src python -m obt check . --expect utf-8 --eol lf
```

上面这条扫描整个仓库时，唯一会报错的是 `samples/`——那是刻意装满
GBK / Big5 / Shift_JIS / CP1252 字节的试验场，本来就"不合规"，
且已在 `.gitignore` 中排除。只查受控文件：

```bash
PYTHONPATH=src python -m obt check src tests scripts .github \
  README.md pyproject.toml uv.lock \
  .editorconfig .gitattributes .gitignore \
  --expect utf-8 --eol lf
```

这条命令就是 CI 的自举门禁（见 `.github/workflows/ci.yml`）：
工具源码、测试、脚本、workflow 自身，以及所有配置文件，都必须
由本工具自己判定为 UTF-8 + LF。判定不符时 `check` 以退出码 1 失败。

也可以反过来核对 git 到底把行尾存成了什么：

```bash
git ls-files --eol        # 期望全部是 i/lf  w/lf
```
