# 参与贡献

欢迎通过 Issues 反馈问题，或提交 Pull Request。

## 运行与测试

后端使用 Python 标准库，前端为本地 HTML、CSS 与原生 JavaScript。安装步骤见 [README](README.md)。

```sh
python3 -m unittest discover -s tests -v
```

这些测试使用临时数据和模拟进程，不需要真实 PikPak 账户。

## 提交问题

请提供 CachyOS、Python 和官方 CLI 版本，复现步骤、预期行为，以及脱敏后的错误信息。授权文件、令牌、私有设备码和任务数据库不应附在公开问题中。

## 代码变更

保持依赖精简。涉及提交、重试、恢复和清理逻辑时，需要验证结果不明的操作不会被重复提交，清理范围仍属于本程序。

项目自有代码采用 [GPL-3.0-only](LICENSE)，第三方资源保留原许可。
