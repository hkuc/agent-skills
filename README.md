# Custom Agent Skills

This repository stores custom Agent Skills in the `skills/` directory.
Each skill must contain a `SKILL.md` with YAML frontmatter containing `name`
and `description`.

## Install from GitHub

Push this repository to GitHub, then install one skill:

```bash
npx skills add <github-owner>/<repository> --skill <skill-name>
```

Install all discovered skills:

```bash
npx skills add <github-owner>/<repository> --all
```

List skills without installing:

```bash
npx skills add <github-owner>/<repository> --list
```

## Add another skill

```bash
npx skills init skills/my-skill
```

Or create `skills/<skill-name>/SKILL.md` manually, then commit the change.

## Skills in this repository

### `multithread-downloader`

用于下载 HTTP/HTTPS 直接文件链接，例如压缩包、安装包、模型文件和直接文件视频。该 skill 支持：

- 默认 8 个下载线程；默认每片 64 MiB
- 按分片断点续传
- 下载完成后逐片校验，再按顺序合并
- 合并后重新校验最终文件大小、分片一致性和可选的可信 SHA-256
- 只有验证并正式发布成功后才清理临时分片
- 自动显示实时百分比、下载速度、预计剩余时间和分片进度
- 支持 `--progress auto|plain|none` 控制进度显示
- 默认发送下载器 User-Agent，支持通过 `--user-agent` 配置浏览器 User-Agent
- 不支持 Range 时自动降级为单线程
- 不读取浏览器 Cookie、不自动登录、不解析网页下载地址

详细使用说明见 [`skills/multithread-downloader/SKILL.md`](skills/multithread-downloader/SKILL.md)。

安装此 skill：

```bash
npx skills add hkuc/agent-skills --skill multithread-downloader
```
