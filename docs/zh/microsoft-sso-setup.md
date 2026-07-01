# 🏢 Microsoft SSO (企业账号) 配置指南

本文适用于通过**公司 Microsoft/Azure AD 账号**登录 Kiro IDE 的用户（即 External IdP 方式，区别于个人 Builder ID 登录）。

---

## 📋 前置条件

1. 已安装 [Kiro IDE](https://kiro.dev/) 并用**公司 Microsoft 账号**完成登录
2. 已安装 Python 3.10+

> **验证是否为 Microsoft SSO 登录：**
> 登录后检查文件 `%USERPROFILE%\.aws\sso\cache\kiro-auth-token.json`，若存在且包含 `"authMethod": "external_idp"`，则说明是 Microsoft SSO 方式。

---

## 🚀 快速初始化（新电脑）

### 第一步：克隆并安装依赖

```bash
git clone https://github.com/Jwadow/kiro-gateway.git
cd kiro-gateway
pip install -r requirements.txt
```

### 第二步：获取 Profile ARN

Profile ARN 是 Kiro 账号的唯一标识，需要从 Kiro IDE 日志中提取。

> **方式 A（推荐）：使用辅助脚本**

```bash
python get_profile_arn.py
```

脚本会自动扫描 Kiro IDE 日志并输出 ARN，例如：
```
Found profileArn: arn:aws:codewhisperer:us-east-1:436207872885:profile/XXXXXX
```

如果想直接更新到 `.env` 文件：
```bash
python get_profile_arn.py --update
```

> **方式 B（手动）：直接读取 credentials 文件**

```bash
# Windows PowerShell
(Get-Content "$env:USERPROFILE\.aws\sso\cache\kiro-auth-token.json" | ConvertFrom-Json).profileArn
```

如果 credentials 文件中没有 `profileArn` 字段，请先打开 Kiro IDE 并进行一次对话，再重试。

### 第三步：创建 .env 配置文件

```bash
cp .env.example .env
```

编辑 `.env`，填入以下**3 个必填项**：

```env
# 必填：访问 Gateway 的密码（自定义）
PROXY_API_KEY="your-secret-key-here"

# 必填：Kiro IDE 凭据文件路径（Windows 路径固定，直接复制即可）
KIRO_CREDS_FILE=%USERPROFILE%\.aws\sso\cache\kiro-auth-token.json

# 必填：上一步获取到的 Profile ARN
PROFILE_ARN=arn:aws:codewhisperer:us-east-1:436207872885:profile/XXXXXX
```

完整推荐配置：

```env
PROXY_API_KEY="your-secret-key-here"
KIRO_CREDS_FILE=%USERPROFILE%\.aws\sso\cache\kiro-auth-token.json
PROFILE_ARN=arn:aws:codewhisperer:us-east-1:436207872885:profile/XXXXXX
SERVER_PORT=7172
DEBUG_MODE=errors
```

### 第四步：启动 Gateway

```bash
python main.py
```

看到以下输出说明启动成功：
```
INFO     | Detected auth type: External IdP (endpoint: https://login.microsoftonline.com/...)
INFO     | Server started on http://0.0.0.0:7172
```

### 第五步：配置 AI 客户端

在 Claude Code、Cursor、Cline 等工具中填入：

| 参数 | 值 |
|------|-----|
| Base URL | `http://localhost:7172` (OpenAI) 或 `http://localhost:7172` (Anthropic) |
| API Key | `.env` 中设置的 `PROXY_API_KEY` |
| Model | `claude-sonnet-4.5` 或 `claude-opus-4.6` 等 |

---

## 🔧 原理说明

Microsoft SSO 认证流程与普通 Kiro 账号不同，需要额外的处理：

```
Microsoft Token (Azure AD)
        ↓
  POST /oauth2/v2.0/token (Microsoft)
        ↓
  New Access Token
        ↓
  POST /generateAssistantResponse (Kiro Runtime)
    + Header: TokenType: EXTERNAL_IDP   ← 关键！缺少此 header 会 403
    + Header: User-Agent: ...           ← 关键！缺少此 header 也会 403
```

本项目对 `kiro/auth.py` 和 `kiro/utils.py` 的改动正是为了处理这两个关键点。

---

## ❓ 常见问题

### Q: 启动时报 `400 profileArn is required`
**原因**：`.env` 中未设置 `PROFILE_ARN`，或设置的值不正确。  
**解决**：重新运行 `python get_profile_arn.py --update` 获取正确的 ARN。

### Q: 请求时持续报 `403 The bearer token is invalid`
**原因**：可能是旧版本代码未包含 `TokenType: EXTERNAL_IDP` header。  
**解决**：确认代码是最新版本（包含本次改动），并**重启 Gateway**。

### Q: 请求时报 `403 User is not authorized to make this call`
**原因**：Access Token 已过期且刷新失败，或 Token 刷新后还未被 Kiro 服务器接受。  
**解决**：
1. 确认 Kiro IDE 处于运行状态（IDE 负责维护 Token 文件）
2. 在 Kiro IDE 中进行一次对话以刷新 Token
3. 重启 Gateway

### Q: Token 多久过期一次？
Microsoft Access Token 通常有效期为 **60-90 分钟**。Gateway 会在过期前自动刷新，无需人工干预。

### Q: 可以在没有 Kiro IDE 的服务器上运行吗？
可以，但需要在 `.env` 中直接配置 `REFRESH_TOKEN`（从 credentials 文件中获取）以便独立刷新 Token：
```env
REFRESH_TOKEN=你的refresh_token值
```
注意：refresh_token 也有过期时间，需要定期从 IDE 同步更新。

---

## 📁 credentials 文件字段说明

`%USERPROFILE%\.aws\sso\cache\kiro-auth-token.json` 文件由 Kiro IDE 自动维护，包含以下关键字段：

| 字段 | 用途 |
|------|------|
| `accessToken` | 当前有效的 Microsoft Access Token |
| `refreshToken` | 用于获取新 Access Token |
| `expiresAt` | Token 过期时间（UTC） |
| `authMethod` | 固定为 `"external_idp"` |
| `tokenEndpoint` | Microsoft token 刷新地址 |
| `clientId` | Azure AD 应用 ID |
| `scopes` | OAuth 权限范围 |
| `profileArn` | Kiro Profile ARN（关键配置项） |

---

*如遇其他问题，请开启调试模式 `DEBUG_MODE=errors` 查看详细日志，或在 [GitHub Issues](https://github.com/jwadow/kiro-gateway/issues) 提交问题。*
