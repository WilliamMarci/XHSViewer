# 工作进度与交接说明（next.md）

> 交接时间：2026-09-02。主程序功能基本完成；**唯一未解决的核心问题：
> 图片在 Ghostty 1.3.1 中无法显示**（见下方「当前阻塞问题」）。

## 已完成功能（均已提交 git）

| 功能 | 说明 |
|------|------|
| 推荐流 | 无限滚动，自动补足一屏内容；卡片式列表（徽标/标题/摘要/作者/赞/评论/时间/图数） |
| 正文渲染 | 标题青粗体、引用斜体+深蓝底、代码琥珀+深底、链接蓝下划线、列表彩色符号、表格、hr |
| 公式 | utftex CLI 优先（行内单行 / 行间多行排版，cases 等）；内置 LaTeX→Unicode 兜底；缓存 512 条 |
| 固定标题栏 | 问题/作者/赞/链接，正文独立滚动；Ctrl+点击链接浏览器打开 |
| n/N 切换回答 | 边界提示（第一个/最后一个/无其他回答/非回答内容）；跨页查找当前回答位置 |
| C 评论弹窗 | 滚动到底自动加载更多 |
| 图片查看器 | 弹窗：+/- 缩放、hjkl/方向键平移、n/p 切换、o 浏览器、Esc 返回；i 查看全部图片 |
| 视频 | 「播放视频」按钮跳浏览器（优先直链） |
| 登录 | 浏览器自动打开 + Firefox/Chrome Cookie 提取 + 手动粘贴降级；Cookie 持久化 |
| SVG 公式 | 默认隐藏（config `show_math_svg`），公式仍以文本渲染 |
| Nerd Font 图标 | 图片/链接/播放/评论用 NF 字形；无 emoji |
| 崩溃日志 | `App.panic`/`_handle_exception`/`sys.excepthook`/`threading.excepthook` → `log/error.log` |
| key panel | 修复 description=None 导致崩溃（绑定描述全部显式给 `""`） |

## 已修复的关键 Bug（调试记录）

1. **滚动不重绘**：`watch_scroll_y/x` 覆写未调用 `super()`，
   `_refresh_scroll()` 不触发 → 滚动位置变了但屏幕不刷新（>10s 才更新）。
   修复：所有 scroll watcher 先调 `super().watch_*(old,new)`。
2. **`self.loading` 变量名冲突**：与 Textual 内置 `loading` reactive 冲突，
   置 True 会盖上一层全屏 LoadingIndicator（蓝屏），且 `Screen._forward_event`
   在 loading 时丢弃鼠标事件 → 滚轮失效。改名 `_loading`。
3. **图片写入导致 writer 线程崩溃**：`driver.write(bytes)` → 线程 `TypeError`
   → 线程死亡 → 队列填满 → 永久卡死。修复：kitty 序列全部转 str。
4. **show keybindings 崩溃**：`Binding(..., None, ...)` 的描述为 None，
   KeyPanel 里 `Text.from_markup(None)` 抛 TypeError。全改 `""`。
5. **内容解析崩溃**：BeautifulSoup `img.get("class")` 返回
   `AttributeValueList`，`.lower()` 抛错；且 worker 崩溃带崩整个 app。
   修复：class 转字符串、`_render_item` 包 try/except 仅提示。
6. **滚动性能**：所有滚动动作 `animate=False, immediate=True`；
   wheel 灵敏度 4 行/次；scroll_to_widget 即时。
7. **space 键崩溃**：`scroll_page_down(immediate=...)` 参数不存在。
   改为 `scroll_to(y+viewport, immediate=True)`。

## 当前阻塞问题：图片无法在 Ghostty 显示（最重要）

### 现象
- 用户 Ghostty 1.3.1（Linux/GTK）：`demo_image.py` 中占位符字符
  （U+10EEEE + diacritic）**显示为普通字符**，图片未渲染。
- 之前的 `a=T`+`a=p`（pinned placement）方案：完全无显示。

### 已做工作
1. 对照 yazi（`yazi-adapter/src/drivers/kgp.rs`）与 Ghostty 源码
   （`src/terminal/kitty/graphics_*.zig`）确认方案：
   - 传输：`q=2,a=T,C=1,U=1,f=32,s=W,v=H,i=ID,m=1;`（zlib RGBA 原始像素），
     后续分块仅 `m=` 参数
   - 显示：每格写入 `U+10EEEE` + 行 diacritic + 列 diacritic，
     前景色（24bit truecolor）= 图片 ID 低 24 位
   - Ghostty 解码链路已验证：`colorToId(fg) → imageById → 虚拟 placement`
     （`IncompletePlacement.init` / `prepKittyVirtualPlacement`）
2. 修复 demo 行对齐（`\r\n`）与状态栏抹掉首行的问题
3. 传输 ID 与前景色 ID 一致（校验 9182=9182）
4. 新增 `demo_image.py --diag`：以 `q=1` 传输并**读取显示终端的协议响应**
   （成功返回 `Gi=<id>;OK`，失败返回错误码），用于定位是「传输失败」
   还是「占位符关联失败」

### 下一步（按优先级）
1. **在用户 Ghostty 运行**：`python demo_image.py --diag logo.png`
   - 若显示 `OK` → 图片已存储，问题在占位符关联/渲染；
     继续对照 Ghostty `placementIterator` 与 `page.zig` 的
     `kitty_virtual_placeholder` 行标记
   - 若显示错误码（如 `EINVAL`/`EDATA`/`ENOENT`）→ 传输格式问题，按错误码修
2. 对照验证：在用户机器跑 `yazi` 图片预览，确认其是否正常；
   正常则逐字节 diff 我方序列与 yazi 输出（可加 `--dump` 保存原始字节）
3. 若 Ghostty unicode placeholder 有版本缺陷：备选
   - 用 `t=d` 直接文件路径 / `t=f` 临时文件媒体（需终端允许）
   - 回退到 ratatui-image 的 halfcell（半块字符）降级方案
   - 或调用外部 `chafa`（Kitty/符号输出）

### 注意
- 测试环境无 Ghostty，只能通过 pty 模拟；pty 中占位符/传输均可验证字节正确，
  但无法验证 Ghostty 渲染。
- `TERM=xterm-ghostty`，`TERM_PROGRAM=ghostty`。
- Ghostty 日志（渲染警告如 "missing image for virtual placement"）：
  Linux 下 `ghostty --debug` 或 journalctl，可辅助定位。

## 其他待办 / 可优化项

- 评论/回答切换、正文渲染已验证，但可再补：长正文分页、评论按楼层折叠
- 图片缓存（/tmp）无过期清理，重启系统自动清
- 公式内置转换器可继续扩充（\begin{align}、矩阵等环境）
- `zhihu_tui.py` 旧文件已弃用，可删除或归档
- pyproject 依赖建议锁定 textual==0.89.1（升级需重测 Image 方案）

## 环境与验证方法

- venv：`.venv/`（Python 3.14）；运行 `pip install -e .`
- 冒烟测试：headless `app.run_test`（见各轮提交前的测试脚本模式）；
  pty 测试用 `pty.fork` + TIOCSWINSZ 模拟终端，
  校验字节（`a=T,C=1,U=1,f=32` / 占位符 / CRLF / 状态栏）
- 代码检查：`.venv/bin/python -m pyflakes zhihu_cli/*.py demo_image.py`
- 崩溃日志：`log/error.log`

## git 历史（主要提交）

```
876faa6 fix: demo 占位符行对齐（CRLF）+ 单行状态栏，与 yazi 逐字节一致
670cdc9 fix: 改用 yazi 同款 Kitty unicode placeholder 方案显示图片
c46aa38 feat: 独立图片显示 demo + Nerd Font 图标
a4e85cb feat: 图片弹窗查看器(缩放/平移)、SVG公式隐藏、符号化标签、崩溃日志
6293aa3 feat: 固定标题栏、公式渲染(utftex)、视频播放、修复快捷键面板崩溃
7731fc6 init: 知乎 CLI 客户端
```