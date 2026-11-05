# 每日简报自动流水线 · 设置说明

这套流水线会让 GitHub Actions 每天北京时间 **07:00** 自动完成：采集（AIHOT + 热榜 + X 中文圈）→ 选题与摘要（GitHub Models 免费额度）→ 按现有模板生成新一期 `briefs/YYYY-MM-DD.html` 并同步更新 `index.html`、`archive.html`、`sw.js` → 提交到 `main` 分支。GitHub Pages 随后自动发布，你的 PWA 打开就是最新一期。**全程不需要任何人点确认。**

文件一览：

| 文件 | 作用 |
|---|---|
| `.github/workflows/daily-brief.yml` | 定时任务定义（每天 07:00 北京时间 + 手动触发） |
| `scripts/collect.py` | 采集三路免费数据源，产出 `data/candidates.json` |
| `scripts/generate.py` | 粗筛 + 调 GitHub Models 选题摘要 + 套模板渲染 |
| `scripts/watchlist.txt` | 「关注账号动态」的 19 人名单，可直接改这个文件增删 |

---

## 一、一次性设置：添加两个 Secret（必须）

采集 X（推特）需要你的登录 Cookie，流水线只通过仓库 Secret 读取，不会写进任何文件或日志。

1. 在**你自己电脑的浏览器**上登录 [x.com](https://x.com)（平时登录着的就行）。
2. 按 `F12` 打开开发者工具 → **应用（Application）** → 左侧 **存储 → Cookie** → 选 `https://x.com`，找到并复制这两项的值：
   - `auth_token`
   - `ct0`
   （也可以用「Get cookies.txt LOCALLY」之类的扩展导出后从文件里找这两项。）
3. 打开本仓库 → **Settings → Secrets and variables → Actions → New repository secret**，添加：
   - Name 填 `X_AUTH_TOKEN`，Secret 填 `auth_token` 的值
   - 再添加一个：Name 填 `X_CT0`，Secret 填 `ct0` 的值

注意事项：

- Cookie 等同于登录态，**只填进 Secret，不要发到聊天、Issue 或任何文件里**。
- 不需要为 GitHub Models 另配 Key：工作流用自带的 `GITHUB_TOKEN` 调用，工作流文件里已声明 `models: read` 权限。
- Cookie 失效的表现：流水线仍会成功出刊，但 X 部分为空、「提醒」区会注明 X 数据不可用。届时重新按上面步骤换一对新的值即可（在导出 Cookie 的浏览器里**不要点退出登录**，退出会作废会话；改密码也会）。

## 二、手动测试一次

仓库页 → **Actions** → 左侧选 **Daily Brief（X 情报站每日简报）** → **Run workflow** → 直接点运行（`backfill_date` 留空表示当天；填 `2026-11-06` 这样的日期可补跑某天）。

跑完看三处：

- 仓库首页应多出一个 `brief: YYYY-MM-DD` 的提交（作者是 github-actions[bot]）；
- 打开站点，最新一期应为当天；
- 这次运行的 **Summary** 页有采集/生成两步的条数统计。

## 三、流水线失败时看哪里

Actions → 点开失败的那次运行：

- **Collect 步骤红**：某个数据源整体挂了（单个源失败不会让步骤失败，只会记录在 Summary 里）。日志末尾与 Summary 会写明哪个源、什么错误。
- **Generate 步骤红**：多半是 GitHub Models 调用失败或免费额度用完（日志里有 HTTP 状态）。额度按天恢复，第二天会自动重试，无需处理；想立刻恢复可手动 Run workflow 再跑一次。
- **没有任何提交但运行成功**：说明当天文件没有变化（极少发生），Summary 里会写「没有变更」。
- 每次运行的 `data/`（候选与元数据）会以 artifact 形式保留 7 天，排查选题问题时可以下载 `brief-data-*` 看 `candidates.json`。

设计原则：采集或生成任何一步硬失败，整个运行就失败，**不会提交半成品**；X 一个源挂掉只会让当期缺 X 内容，简报照常出刊并在「提醒」区注明。

## 四、如何停用

- **临时停**：Actions → 该 workflow 右上角 `···` → **Disable workflow**。恢复时同样位置 Enable。
- **只停定时、保留手动**：编辑 `.github/workflows/daily-brief.yml`，删掉 `schedule:` 两行即可。
- **改时间**：改 `cron:` 的值（注意是 UTC，北京时间 = UTC+8，如北京时间 08:00 对应 `0 0 * * *`）。

## 五、关于生成质量的预期（先说清楚）

- 选题与摘要由 **GitHub Models 免费额度**的 `openai/gpt-4o-mini` 完成：覆盖与速度没问题，但文笔和判断力**达不到人工精选的水准**——偶尔会有摘要不够准、分区放错、重点选得一般的情况。这是零成本全自动的固有代价。
- 免费额度有每日请求数与单请求 token 上限；流水线已做本地粗筛并把调用压到 3 次/天，正常不会超限。超限当天生成步骤会失败、次日自动恢复，**不会产生费用**（GitHub Models 免费层不绑卡扣费）。
- 模板、分区、条数目标（造物 20 / 福利 20 / 其余各 10，不够不凑数）、排除清单、来源标签规则都写死在 `scripts/generate.py` 顶部的配置与系统提示里，想调整口味改那里即可。
- 名单增删：直接编辑 `scripts/watchlist.txt`（一行一个：`handle | 显示名 | 定位`）。
