# 知乎 CLI 客户端（ZhihuViewer）— 项目说明

## 项目要求（来自用户的迭代需求）

1. **知乎 CLI 客户端**：推荐流卡片式浏览，像网页一样无限滚动
2. **终端内图片查看**：目标终端为 **Ghostty**（Kitty 图片协议），点击展开查看
3. **HTML 元素渲染区分**：不同元素用不同颜色 / 加粗 / 背景色
   （标题、引用、代码、链接、列表、公式）
4. **浏览器登录**：自动打开浏览器登录页；自动从浏览器读取 Cookie
   （Firefox 直读 / Chrome 系解密）；失败降级为手动粘贴 Cookie
5. **卡片展开全文**：每个条目一张卡片，Enter/点击展开正文，正文可滚动
6. **图片独立展开按钮**：每张图片独立小按钮
7. **UI 简单现代、小按钮**：单行紧凑按钮
8. **回答页切换**：`n` / `N` 在同一问题下切换回答（正确处理首尾边界），
   滚动到回答底部时弹提示
9. **评论弹窗**：`C` 打开评论（滚动自动加载更多）
10. **固定标题栏**：问题 / 作者 / 赞 / 链接，正文独立滚动；
    **Ctrl+点击链接**在浏览器打开
11. **修复 show keybindings 崩溃**（命令面板 ctrl+p → Show keys and help panel）
12. **图片改为弹窗查看器**：`+`/`-` 缩放，`hjkl`/方向键 平移，
    `n`/`p` 切换同组图片，`Esc` 返回，`o` 浏览器打开
13. **视频**：点击跳转浏览器播放（优先播放直链）
14. **公式渲染**：LaTeX 公式转 Unicode（优先 utftex CLI；行内单行、
    行间多行排版块；内置转换器兜底）
15. **行间公式 SVG 隐藏**：config.json 中 `show_math_svg`（默认 false）
16. **不使用 emoji**：改用 Nerd Font 图标（NF 字形）与几何符号
17. **崩溃日志**：异常/panic/线程异常写入 `log/error.log`
18. **独立图片 demo**：`demo_image.py` 单独验证终端图片显示
19. **Git 管理代码版本**

## 目录结构

```
ZhihuViewer/
├── zhihu_tui.py            # 遗留的旧版单文件 TUI（URL 浏览，已弃用，保留参考）
├── demo_image.py           # 独立图片显示 demo（Kitty U=1 占位符方案）
├── pyproject.toml          # 打包配置，入口命令 `zhihu`
├── README.md               # 使用说明
├── log/                    # 崩溃日志（error.log）
├── vendor/
│   ├── intro.md            # 本文档
│   └── next.md             # 工作进度与交接说明
└── zhihu_cli/
    ├── __init__.py
    ├── __main__.py         # 入口：登录检测、终端探测、启动 App
    ├── config.py           # 用户配置（~/.config/zhihu-viewer/config.json）
    ├── net.py              # 知乎 API：推荐流 / 问题回答 / 评论 / 登录 / Cookie
    ├── htmlparse.py        # HTML → 带样式的 rich Text 块（含公式/图片/SVG 处理）
    ├── mathtex.py          # LaTeX → Unicode 转换器（utftex CLI + 内置兜底）
    ├── kitty.py            # Kitty 图片协议：探测 / U=1 传输 / 占位符 / 删除
    ├── ui.py               # Textual 界面：卡片流 / 正文 / 图片查看器 / 评论
    └── styles.tcss         # 主题样式
```

## 技术栈

| 组件 | 版本/说明 |
|------|-----------|
| Python | 3.14（venv：`.venv/`） |
| Textual | **0.89.1**（锁定；8.x 无 Image 支持且 API 变更；图片方案为自研） |
| requests / bs4 / lxml | 网络与 HTML 解析 |
| Pillow | 图片处理（resize / RGBA 原始像素） |
| pycryptodome | Chrome Cookie 解密（Linux） |
| utftex | 外部 CLI（`/usr/local/bin/utftex`，Node 版），公式排版 |
| Ghostty | 目标终端（Kitty 图片协议） |

## 运行

```bash
.venv/bin/zhihu                 # 启动
.venv/bin/zhihu --login         # 强制重新登录
.venv/bin/zhihu --cookies "d_c0=xxx; z_c0=yyy"
.venv/bin/python demo_image.py [文件|URL]   # 图片显示 demo
.venv/bin/python demo_image.py --diag [文件|URL]  # 诊断：显示终端协议响应
```

## 数据与配置位置

| 项目 | 路径 |
|------|------|
| Cookie | `~/.config/zhihu-viewer/cookies.json`（旧 `cookies.txt` 兼容） |
| 配置 | `~/.config/zhihu-viewer/config.json`（`show_math_svg`） |
| 图片缓存 | `/tmp/zhihu-viewer/img/` |
| 崩溃日志 | `log/error.log`（项目目录） |

## 主要 API

- 推荐流：`GET /api/v3/feed/topstory/recommend`（`action=down&desktop=true&page_number=N`，
  分页跟随响应 `paging.next`）
- 问题回答：`GET /api/v4/questions/{qid}/answers`（include/offset 分页）
- 评论：`GET /api/v4/answers/{aid}/comments`（offset 分页）
- 登录校验：`GET /api/v4/me`
- 公式：知乎正文为 `<img src="...equation?tex=.." alt="LaTeX" eeimg="1">`
  （`eeimg="1"` = 行内公式；`\begin{...}` 强制按行间处理；SVG 公式默认隐藏）