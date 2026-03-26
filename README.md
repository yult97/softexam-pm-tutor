# softexam-pm-tutor

基于《信息系统项目管理师教程》的教材检索问答 Skill，适合用于高项备考中的概念解释、章节定位、考点总结、题目回查，以及“教材优先 + 官方网站补充延展”的混合回答场景。

## 特性

- 教材优先：先按教材给出结论、依据和页码
- 官方补充：默认补充高质量官方网站内容，如 PMI、Scrum Guides、PeopleCert
- 单入口问答：日常问答默认只用 `scripts/answer_question.py`
- 题型适配：支持定义题、总结题、比较题、题目回查
- 共享记忆：支持记录用户纠正，并自动刷新别名、页码提示、题型偏好、联网来源偏好

## 适用场景

- “按教材回答项目章程是什么”
- “WBS 分解需要注意什么”
- “质量保证和质量控制的区别”
- “这道题对应教材哪一章哪一页”
- “先按教材回答，再结合 PMI 官方资料补充”

## 目录结构

- [SKILL.md](/Users/yubobo/.codex/skills/softexam-pm-tutor/SKILL.md)：Skill 主说明
- [scripts](/Users/yubobo/.codex/skills/softexam-pm-tutor/scripts)：检索、问答、反馈、共享记忆构建脚本
- [references](/Users/yubobo/.codex/skills/softexam-pm-tutor/references)：索引、目录、别名表、题型规则、联网来源策略
- [shared_memory](/Users/yubobo/.codex/skills/softexam-pm-tutor/shared_memory)：共享记忆文件
- [evals](/Users/yubobo/.codex/skills/softexam-pm-tutor/evals)：回归测试样例

## 环境要求

- `python3`
- 能运行本地 Python 脚本的代理环境
- 如需联网补充，运行环境需要允许网络访问

如果目标机器还没有 Python 3，可使用仓库内的安装脚本：

- macOS / Linux: `scripts/install_python3.sh`
- Windows PowerShell: `scripts/install_python3.ps1`

## 安装

如果你使用 Codex，可将仓库放到本地技能目录下：

```bash
mkdir -p ~/.codex/skills
git clone <your-repo-url> ~/.codex/skills/softexam-pm-tutor
```

如果你使用其他支持本地 Skill/Agent 目录的环境，也可以直接复制整个文件夹使用。

你也可以直接在支持 Skill 安装的代理环境里这样说：

- `Codex`：`帮我安装这个 skill，github 地址：<your-repo-url>`
- `Claude Code`：`帮我安装这个 skill，github 地址：<your-repo-url>`
- `OpenClaw`：`帮我安装这个 skill，github 地址：<your-repo-url>`

如果你的环境支持从 GitHub 仓库直接安装 Skill，这种说法通常就够了。

如果对方环境还没有 Python 3，也可以继续补一句：

- `请先运行仓库里的 Python 安装脚本，再安装这个 skill。`

## 快速开始

如需先安装 Python 3：

```bash
bash scripts/install_python3.sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_python3.ps1
```

日常问答默认入口：

```bash
python3 scripts/answer_question.py "项目章程是什么？"
```

底层检索复核：

```bash
python3 scripts/search_book.py "项目章程是什么？" --top-k 8 --json
```

仅在索引缺失或教材更新时重建索引：

```bash
python3 scripts/build_index.py
```

## 回答模式

默认采用：

1. 教材答案
2. 教材依据和页码
3. 高质量官方网站补充延展

只有在以下场景下会关闭联网补充：

- 用户明确要求“只按教材回答”
- 用户明确要求“不要联网”
- 纯选择题、填空题、回忆型简答题，联网补充会干扰作答

## 教材资源说明

仓库默认包含预构建的教材索引文件：

- [book_chunks.jsonl](/Users/yubobo/.codex/skills/softexam-pm-tutor/references/book_chunks.jsonl)

因此大多数情况下，克隆后即可直接问答。

原始 PDF 没有随仓库提交，原因是本地教材 PDF 文件体积较大，超出 GitHub 普通仓库单文件限制。如果你需要自行重建索引，请将教材 PDF 放到：

```text
assets/信息系统项目管理师教程(可搜索版).pdf
```

然后执行：

```bash
python3 scripts/build_index.py
```

## 自学习与共享记忆

当用户明确纠正回答内容或联网来源时，可以记录反馈：

```bash
python3 scripts/record_feedback.py "项目章程是什么" \
  --feedback "以后这类题优先查 PMI，不要查转载站" \
  --issue-type web_source_preference \
  --preferred-domain "pmi.org"
```

默认会自动完成：

1. 写入本地反馈日志
2. 同步到共享反馈池
3. 自动重建共享记忆

共享记忆目前包括：

- `alias_memory.json`
- `retrieval_memory.json`
- `pattern_memory.json`
- `web_source_memory.json`

## 隐私与分享

仓库默认不提交这些本地数据：

- `learnings/feedback_events.jsonl`
- `learnings/promotion_candidates.md`
- `shared_memory/feedback_pool.jsonl`

这样可以避免把个人学习记录或原始反馈一并分享出去。

## 回归测试

回归样例位于：

- [evals.json](/Users/yubobo/.codex/skills/softexam-pm-tutor/evals/evals.json)

你可以把它作为后续迭代时的最小验收集。

## 适合谁用

- 备考信息系统项目管理师的学习者
- 想用教材原文和页码做依据的答题场景
- 希望在教材基础上，再补一层官方实践解释的用户

## 说明

如果用于考试，请始终以教材表述为准；联网补充只作为理解和延展，不替代教材答案。
