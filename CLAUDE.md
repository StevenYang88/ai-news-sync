# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 工作区用途

本目录是 lark-cli（飞书命令行工具）的使用工作区，不包含可开发代码。
lark-cli 通过 npm 全局安装，配置文件位于 `~/.lark-cli/config.json`。

## lark-cli 当前状态

- 版本: 1.0.39（通过 `npm install -g @larksuite/cli` 安装）
- 品牌: feishu
- 身份: user（用户态，杨阳）
- 用户 open_id: `ou_be3a84d1a1af94f57e9bed55f30b7f88`

## 常用命令

命令分为三层：快捷命令（`+` 前缀）→ API 命令 → 原始 API。

### 日历
```bash
lark-cli calendar +agenda --start "2026-05-25T00:00:00+08:00" --end "2026-06-01T00:00:00+08:00" --format pretty
lark-cli calendar calendars list
```

### 消息搜索与发送
```bash
lark-cli im +messages-search --query "关键词" --format pretty
lark-cli im +messages-send --user-id ou_xxx --text "消息内容"
lark-cli im +chat-list
```

### 任务
```bash
lark-cli task +get-my-tasks --format pretty
```

### 权限管理
```bash
lark-cli auth login --scope "<scope>"  # 补充授权，命令阻塞等待浏览器授权
lark-cli auth status                   # 查看当前授权状态
lark-cli auth scopes                   # 查看应用可用权限
```

## 授权流程

当命令报 `missing_scope` 时，需要补充授权：
1. 运行 `lark-cli auth login --scope "<scope>"`（后台运行）
2. 获取输出的 `verification_uri_complete` URL
3. 用户浏览器打开 URL 完成授权
4. 授权完成后重新执行原命令

已授权的写入类 scope 包括 `im:message.send_as_user`、`search:message` 等。
