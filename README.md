# translate-docs —— GitHub Action（结构保真的文档本地化）

把一个 docs 目录翻译成目标语言：**代码围栏、表格、链接、frontmatter 逐字节不变**，
并且**术语表违约会让 CI 失败**（而不是悄悄用错词）。

## 它是怎么跑的

```
GitHub Actions job
  └── composite action (action.yml)          ← 只做参数拼装 + 汇总
        └── python3 translate_docs.py        ← 真正的入口脚本，只用标准库（无 pip 安装）
              ├── POST /v1/quote             ← 先报价（免费），拿到字符数
              ├── 超过 --max-chars 就直接失败 ← 防止写错 glob 花掉预算
              ├── POST /v1/translate         ← 真实翻译（含术语表）
              ├── 写出 README.zh-CN.md       ← 或 zh-CN/README.md（output-mode=subdir）
              ├── 有 violations → ::error 注解（--fail-on-violation 时 job 失败）
              └── 写 $GITHUB_STEP_SUMMARY    ← 在 PR 页面显示文件数/字符数/花费
```

设计取舍（都是有理由的，别改掉）：

| 决定 | 理由 |
|---|---|
| **只用标准库** | CI 里不引入依赖供应链，也不需要 `pip install` 那一步 |
| **先 quote 再 translate** | 一次写错的 glob 不该变成账单；`--max-chars` 把它变成干净的失败 |
| **术语违约 = 构建失败** | 这是与"随便一个翻译 API"的全部区别：术语漂移在 PR 里可见 |
| **任何异常都 fail-closed** | 非 2xx / 非 JSON / 写不出文件 → 立即退出，不做"部分成功" |

## 用法

```yaml
# .github/workflows/docs-i18n.yml
name: docs i18n
on:
  pull_request:
    paths: ["docs/**/*.md"]

permissions:
  contents: write

jobs:
  translate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }        # changed-since 需要历史
      - uses: retiringsoon/md-translate-action@v1   # ← 发布后的引用方式
        with:
          api-key: ${{ secrets.MDTRANSLATE_API_KEY }}
          paths: |
            docs/**/*.md
          target-lang: into Simplified Chinese
          lang-slug: zh-CN
          glossary: .github/glossary.csv
          changed-since: ${{ github.event.pull_request.base.sha }}
          fail-on-violation: "true"
      - name: Commit translations
        run: |
          git config user.name  "docs-bot"
          git config user.email "docs-bot@users.noreply.github.com"
          git add -A
          git diff --cached --quiet || git commit -m "docs: translated zh-CN"
          git push
```

在本仓库内先试（尚未发布成独立 Action 时）：

```yaml
      - uses: ./.github/actions/translate-docs   # 把本目录放到这个路径
        with: { api-key: "${{ secrets.MDTRANSLATE_API_KEY }}" }
```

术语表两种写法（`glossary` 输入指路径）：

```csv
# .github/glossary.csv（# 开头为注释）
grinder,磨核
prompt,提示词
```

```json
// .github/glossary.json
[{"source": "grinder", "target": "磨核"}, {"source": "prompt", "target": "提示词"}]
```

## 输入 / 输出

| 输入 | 默认 | 说明 |
|---|---|---|
| `api-key` | 必填 | 存成 repository secret |
| `api-url` | 已部署地址 | API 基址 |
| `paths` | `docs/**/*.md` | 多行 glob |
| `target-lang` | `into Simplified Chinese` | 传给模型的指令 |
| `lang-slug` | `zh-CN` | 产出文件名/目录后缀 |
| `glossary` | 空 | 术语表路径（CSV 或 JSON） |
| `output-mode` | `sibling` | `sibling`=README.zh-CN.md / `subdir`=zh-CN/README.md |
| `changed-since` | 空 | 只翻该 ref 之后变更的文件（PR 场景） |
| `fail-on-violation` | `true` | 术语未遵守 → job 失败 |
| `max-chars` | `200000` | 单次运行字符上限（防误配 glob 爆预算） |
| `dry-run` | `false` | 只报价不翻译 |

输出：`characters`（本次字符数）、`summary`（逐文件摘要）。
job summary 里会显示文件数、字符数、违约数与按 $0.03/1k 估算的花费。

## 本地自测

```bash
python3 -m unittest discover -s tests -v      # 5 项确定性测试（桩 API：成本护栏/术语闸门/fail-closed/缺 key）
python3 tests/test_action.py --real           # 打真实 API 的端到端冒烟
```

## 为什么这条比"再上架一个 API"更值得做

RapidAPI / Poe 只解决**收款**，不解决**获客**；而免费 Action 能带来**真实的试用**：
它在客户的 PR 里产出可审阅的译文与术语报告——这就是"我们做得对不对"的第一个事实。
商业上它也是最优形态：**Action 免费、付费走后端 API**，因此绕开 GitHub Marketplace 的
verified publisher 门槛与 25% 抽成，我们只付 Stripe 的 2.9%+$0.30。

## 实测（2026-10-05，真实文档）

| 语料 | 可售字符 | 块数 | 耗时 | 术语违约 | 结构校验 |
|---|---|---|---|---|---|
| 3 份真实 AI4Science 文档 | 16,498 | 194 | 59.9s（经公网 Funnel） | **0** | 代码围栏**逐字节一致**、标题/列表/表格行数 1:1 |

（以上经真实 API，非桩；同日在单文档上测得的吞吐约 236 字符/秒。）
