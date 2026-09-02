# 工作进度与交接说明（next.md）

> 交接时间：2026-09-02。主程序功能基本完成；图片显示问题已定位根因并修复
> （见下方「图片无法显示：已定位根因并修复」），请在真机 Ghostty 复核。

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

## 图片问题的旧诊断记录（已修复，留档备查）

- 现象：Ghostty 1.3.1 中占位符（U+10EEEE + diacritic）显示为普通字符。
- 之前已做：对照 yazi `kgp.rs` 与 Ghostty `graphics_*.zig`，确认传输格式
  `q=2,a=T,C=1,U=1,f=32,s=W,v=H,i=ID,m=1`；校验 ID 与前景色一致；新增
  `demo_image.py --diag`（q=1 读终端响应）。
- 诊断手段（真机复核时仍可用）：
  - `python demo_image.py --diag logo.png`：`OK` → 图片已存储，
    问题在占位符关联；错误码（`EINVAL`/`EDATA`/`ENOENT`）→ 传输格式。
  - `ghostty --debug` 或 journalctl 看 "missing image for virtual placement"。
- 最终根因与修复见下方「图片无法显示：已定位根因并修复」。

## 图片无法显示：已定位根因并修复（对照 Ghostty 源码）

### 根因（2026-09-02 对照 Ghostty 源码逐字节确认）

1. **zlib 载荷没有声明压缩方式（最终阻断显示的 bug）**：
   `transmit_rgba` 对 RGBA 执行了 `zlib.compress()`，但控制数据中漏掉 `o=z`。
   Ghostty 因而把压缩流当作未压缩 RGBA 读取，载荷长度和像素格式不匹配；同时
   `q=2` 静默了错误响应，使问题被误判为 placement 关联失败。修复：所有压缩的
   `a=t` / `a=T` 首块均添加 `o=z`。
   真机 Ghostty 1.3.1 运行 `demo_image.py` 后已确认图片成功显示。
2. **单元格尺寸查询命令错误**：原实现发送裸 Kitty APC `q=2`，这不是尺寸查询，
   Ghostty 会报告 `EINVAL: dimensions required`。已改为标准 `CSI 16 t`，解析
   `CSI 6;height;width t` 响应。
3. **缺少 virtual placement 注册**：
   Ghostty/kitty 的 unicode placeholder 方案要求「先 `a=t,U=1` 传输图片，再
   `a=p,U=1,i=..,c=..,r=..` 注册网格」，之后写入 `U+10EEEE`+变音符 才渲染。
   旧代码用 `a=T,U=1`（无 c/r）依赖终端自动推算网格；Ghostty 1.3.x 该路径
   不稳定 → `grid()` 返回 `PlacementMissingPlacement` → 占位符当普通字符显示。
   修复：`transmit_rgba` 改为 `a=t,U=1`，新增 `kitty.virtual_placement(id,cols,rows)`
   （`a=p,U=1,i,c,r`），TUI 与 demo 在传输后、写占位符前调用。
4. **DIACRITICS 表与终端不一致**：旧表 314 项，且从 index 62 起错位
   （混入 0x618/0x619/0x61a/0x65f…；`\u10a0f` 等 5 位十六进制被 Python `\u`
   截断成错误字符）。终端用自己那张 297 项表反查 index，错位→行/列映射全错。
   已按 Ghostty `graphics_unicode.zig` 的 297 项表完全重写，并逐项比对一致。
5. **像素尺寸与网格错位（缩放导致）**：旧代码先算 `disp_w/disp_h` 再 `//cw`
   取网格，缩放后两张尺寸对不上 → Ghostty 按自身逻辑再缩放/加黑边。
   修复：先取 `cols/rows`，再令 `disp_w=cols*cw, disp_h=rows*ch`，1:1 映射。
6. **Nerd Font 图标码点错误**：`\uf0437` 等 5 位十六进制同样被 `\u` 截断，
   且 v2 旧码点在 Nerd Font v3 已重分配。已改用 v3 码点（`\U000f02e9` 等），
   并用本机字体 cmap 逐项验证存在。

### 验证
- `.venv/bin/python -m pyflakes zhihu_cli/*.py demo_image.py` 通过
- headless `app.run_test` 冒烟：ContentScreen/评论区/图片按钮/选择模式 均可
- `DIACRITICS` 与 Ghostty 297 项表 `==` 比对通过
- 请在真机 Ghostty 再跑：`.venv/bin/python demo_image.py`（应能看到图片；
  `--diag` 仍可看传输响应码）

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
