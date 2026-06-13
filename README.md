# 🏦 银行积存金价监控 - 云端版 (GitHub Actions)

> **完全免费 · 无需服务器 · 无需电脑开机 · 自动定时运行**

## ✨ 功能特点

- ⏰ **工作日自动监控** — 每天自动检查 4 次（9:00 / 12:30 / 15:00 / 20:00）
- 📧 **价格波动邮件提醒** — 变动超阈值立即发送精美HTML邮件
- 🌍 **多数据源自动切换** — Metals-API / Metal-Price-API / Frankfurter
- 💰 **积存金价格估算** — 基于国际现货金价 + 银行溢价计算
- 📊 **完整历史记录** — 价格趋势可追溯
- 🔒 **安全凭据管理** — 邮箱密码通过 GitHub Secrets 加密存储

---

## 🚀 3 步部署（5分钟搞定）

### 第1步：Fork 此仓库

点击右上角 **Fork** 按钮，将此仓库复制到你的 GitHub 账号下

### 第2步：配置 Secrets（邮箱密钥）

1. 进入你 Fork 的仓库 → **Settings** (设置)
2. 左侧菜单选择 **Secrets and variables** → **Actions**
3. 点击 **New repository secret** (新建仓库密钥)
4. 添加以下 3 个 Secret：

| Name | Value | 说明 |
|------|-------|------|
| `SMTP_USER` | `你的邮箱@163.com` | 发件人邮箱 |
| `SMTP_PASS` | `xxxxxxxxxxxxxx` | SMTP授权码（非登录密码）|
| `RECIPIENTS` | `收件人@xxx.com` | 收件人（多个用逗号分隔）|

> 💡 **获取163授权码**: 登录 mail.163.com → 设置 → POP3/SMTP/IMAP → 开启SMTP → 生成授权码

### 第3步：启用 Actions 并测试

1. 进入 **Actions** 标签页
2. 点击 **I understand my workflows, go ahead and enable them**
3. 点击左侧 **银行积存金价监控 & 邮件提醒**
4. 点击 **Run workflow** → **Run workflow** 手动触发一次测试
5. 检查你的邮箱是否收到提醒邮件 ✅

---

## ⏰ 定时运行时间表

| 时间 (北京时间) | UTC时间 | 说明 |
|----------------|---------|------|
| 09:00 | 01:00 | 开盘前检查 |
| 12:30 | 04:30 | 午盘检查 |
| 15:00 | 07:00 | 午后检查 |
| 20:00 | 12:00 | 盘后检查 |

> 仅在 **周一至周五** 运行（周末休市）

### 自定义运行时间

编辑 `.github/workflows/gold-monitor.yml` 中的 `schedule` 部分：

```yaml
schedule:
  # 格式: 分 时 日 月 周 (0=周日, 1=周一, ..., 5=周五)
  - cron: '0 1 * * 1-5'    # 北京时间 9:00, 工作日
```

---

## 📧 邮件效果示例

收到邮件时你会看到：

> 🏦 **积存金价格波动提醒**
> 
> | 项目 | 数值 |
> |------|------|
> | 当前价格 | **¥918.24/g ↑** |
> | 上次价格 | ¥916.50/g |
> | 价格变动 | **+1.74 元/克 (+0.19%)** |
> | 国际现货 | $2372.50/oz ≈ ¥908.12/g |
> | 数据源 | Metals-API.com |
> | 更新时间 | 2026-06-14 09:00:05 |

---

## ⚙️ 自定义配置

### 修改价格阈值

在仓库 Settings → Secrets 中添加：

| Secret名 | 默认值 | 说明 |
|----------|--------|------|
| `PRICE_THRESHOLD` | `1.0` | 价格变动阈值(元/克)，超过则发邮件 |
| `PERCENT_THRESHOLD` | `0.1` | 百分比阈值(%)，超过也发邮件 |
| `MAX_ALERTS_PER_HOUR` | `6` | 每小时最大提醒数 |

### 修改邮箱服务商

编辑 `cloud_gold_monitor.py` 中的默认值，或通过 Secrets 覆盖：

| 服务商 | SMTP地址 | 端口 |
|--------|---------|------|
| 163网易 | smtp.163.com | 465 |
| QQ邮箱 | smtp.qq.com | 465 |
| Gmail | smtp.gmail.com | 587 |
| Outlook | smtp.office365.com | 587 |

---

## 📁 文件说明

```
├── .github/
│   └── workflows/
│       └── gold-monitor.yml    # GitHub Actions 工作流定义
├── cloud_gold_monitor.py       # 云端版监控脚本 (核心)
├── README.md                   # 本文件
├── logs/                       # 运行日志 (自动生成)
├── price_history.json          # 价格历史记录 (自动生成)
└── last_state.json             # 上次状态缓存 (自动生成)
```

---

## 🔍 查看运行日志

1. 进入仓库 **Actions** 页面
2. 点击最近的运行记录
3. 左侧查看各步骤执行情况
4. 底部 **Artifacts** 区域下载完整日志

---

## ❓ 常见问题

### Q: 收不到邮件？
1. 检查 Secrets 是否正确填写（注意不要有空格）
2. 检查垃圾邮件文件夹
3. 确认授权码未过期（163授权码长期有效）

### Q: 价格准确吗？
- 国际现货金价来自专业API，**非常准确**
- 积存金价格为**估算值**（现货价 + ~10元银行溢价），误差通常在 ±5 元/克
- 如需精确银行价格，请配合本地浏览器模式使用

### Q: 如何修改检查频率？
编辑 `.github/workflows/gold-monitor.yml` 中的 `cron` 表达式即可

### Q: 会产生费用吗？
- **完全免费！** GitHub Actions 公开仓库免费额度：每月 2000 分钟，本任务每次约 1 分钟
- 每天 4 次 × 30 天 = 120 分钟/月，远低于免费额度

---

## 📄 许可证

MIT License - Free to use, modify, distribute

---

*Made with ❤️ by ToDesk AI Assistant*
*Powered by GitHub Actions*
