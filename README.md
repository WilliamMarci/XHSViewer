# 知乎 CLI（ZhihuViewer）

终端里的知乎：推荐流卡片式浏览 + 展开看全文 + 终端内直接查看图片。

在 **Ghostty**（或任何支持 Kitty 图片协议的终端）中运行效果最佳：点击图片的展开按钮，
图片会以内联方式直接渲染在终端里，随内容一起滚动。

## 功能

- **推荐流**：像网页一样无限滚动，滚动到底部自动加载下一页，启动时会自动补足内容保证可滚动
- **卡片式列表**：每条内容一张卡片（回答 / 文章 / 视频 / 提问 / 想法），显示
  类型徽标、标题、摘要、作者、赞同、评论、时间、图片数量
- **展开全文**：按 Enter 进入正文，不同 HTML 元素用不同颜色 / 加粗 / 背景色渲染：
  - 标题：亮青色加粗
  - 引用（blockquote）：斜体 + 深蓝背景
  - 代码（pre/code）：琥珀色文字 + 深色背景
  - 链接：蓝色下划线
  - 列表：彩色项目符号 / 序号
- **图片独立展开**：每张图片一个独立的单行小按钮，逐个展开 / 收起；`i` 一键全部展开
  （Kitty 图片协议内联渲染，Ghostty 原生支持）
- **登录**：自动打开浏览器登录，自动从浏览器读取 Cookie（Firefox / Chrome 系），
  失败自动降级为手动粘贴 Cookie
- **滚动**：鼠标滚轮 / 触控板、方向键、`j`/`k`、PageUp/PageDown、Home/End 全部即时响应
- **加载动画**：加载时左下角显示小型 spinner（如 `⠋ 加载中…`），类似 opencode
- **缓存**：图片下载缓存存放在 `/tmp/zhihu-viewer/img/`

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 使用

```bash
.venv/bin/zhihu            # 启动（首次会自动登录）
.venv/bin/zhihu --login    # 强制重新登录
.venv/bin/zhihu --cookies "d_c0=xxx; z_c0=yyy"   # 直接提供 Cookie
```

### 按键

推荐流：

| 按键 | 功能 |
|------|------|
| `j` / `k` / `↑` / `↓` | 选择卡片 |
| `Enter` / 鼠标点击 | 展开卡片 |
| `r` | 刷新 |
| `o` | 在浏览器中打开 |
| `g` / `G` | 顶部 / 底部 |
| `q` | 退出 |

正文页：

| 按键 | 功能 |
|------|------|
| `Esc` / `q` | 返回推荐流 |
| `j` / `k` / `空格` | 滚动 |
| `n` / `N`（Shift+n） | 下一个 / 上一个回答（同一问题下切换，自动处理首尾边界） |
| `C` | 弹出该回答的评论页（滚动到底自动加载更多） |
| `o` | 在浏览器打开原文 |
| `i` | 全部图片展开 / 收起 |

滚动到回答底部时会弹出小提示（`n` 下一个回答 · `N` 上一个 · `C` 评论）。

### 登录说明

1. 自动扫描本机浏览器（Firefox 优先，Chrome/Chromium/Edge/Brave 会尝试解密）读取知乎 Cookie
2. 找不到时自动用系统浏览器打开 `zhihu.com/signin`，等待登录（最多 90 秒，期间每 2 秒重试）
3. 仍失败则提示手动粘贴 Cookie（浏览器开发者工具中复制 `zhihu.com` 的 Cookie 字符串）

Cookie 保存在 `~/.config/zhihu-viewer/cookies.json`；旧的 `cookies.txt`（Netscape 格式）也兼容。

## 图片说明

图片通过 **Kitty 图片协议** 渲染（Ghostty 原生支持）。启动时会自动探测终端；
若终端不支持（如非 Ghostty / kitty），图片按钮会提示降级，可用 `o` 在浏览器查看。

## 项目结构

```
zhihu_cli/
├── kitty.py       # Kitty 图片协议：单元尺寸查询、发送、删除
├── net.py         # 知乎 API（推荐流 / 登录 / Cookie 提取与持久化）
├── htmlparse.py   # HTML -> 带样式的 rich Text（颜色 / 加粗 / 背景色）
├── ui.py          # Textual 界面：卡片流、正文页、内联图片控件
├── styles.tcss    # 主题样式
└── __main__.py    # 入口
```