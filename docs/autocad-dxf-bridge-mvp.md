# AutoCAD / DXF Bridge MVP

本 MVP 是 KORYAO 的 CAD / DXF 无头生成 bridge 原型。它建立安全、可测试的 CAD plan 合约，并可在明确确认后真实生成和审计测试 DXF，而不是直接控制 AutoCAD。

## 为什么先做 DXF plan

直接控制 AutoCAD / DWG 会涉及本机授权、商业图纸、客户路径、COM 状态、窗口焦点和真实项目输出。DXF plan 能先把结构化几何、图层、单位和文本变成可验证数据，适合作为 Codex 后续生成 CAD 图形的中间层。

## 当前安全机制

- 默认 `dry_run=True`，不写 DXF 文件。
- 不调用真实 AutoCAD。
- 不打开 DWG。
- 不扫描用户目录。
- `dry_run=False` 时只允许写到 `examples/cad/output/`。
- 真实写入必须同时提供 `confirm_write=True`，且不会覆盖已有批次。
- DXF 先写临时文件，再由 `ezdxf` 重新读取并执行 audit；验证通过后，从该回读文档生成 `<name>.preview.svg`。
- DXF、无头 SVG 预览和 `<name>.manifest.json` 作为同一批次交付；任一产物失败就回滚当前批次。
- manifest 记录 DXF 与 SVG 的相对路径、字节数和 SHA-256，以及实体数、audit 结果、SVG 路径数和外部引用检查结果。
- 输出统一经过 KORYAO sanitizer，不输出真实用户目录。

## 支持的实体类型

| 类型 | 必要字段 |
| --- | --- |
| `line` | `start`, `end`, `layer` |
| `polyline` | `points`, `layer` |
| `circle` | `center`, `radius`, `layer` |
| `rectangle` | `x`, `y`, `width`, `height`, `layer` |
| `text` | `position`, `value`, `height`, `layer` |

## 可选依赖

`ezdxf` 是可选依赖。没有安装时：

- `status()` 仍可返回 bridge 状态。
- `validate_cad_plan()` 和 `summarize_plan()` 仍可运行。
- `write_dxf(..., dry_run=True)` 仍可运行。
- `write_dxf(..., dry_run=False)` 会返回 warning 和 next_steps，不会崩溃。

安装 `ezdxf` 后，可以生成公开安全示例：

```powershell
python examples\cad\generate_dxf_plan.py `
  --confirm-write `
  --output starbridge_public_demo.dxf
```

成功条件不是“写文件调用没有报错”，而是 DXF、SVG 预览与 manifest 都存在；DXF 可重新读取、audit 错误数为 0、实体数与已校验 plan 一致；SVG 可解析、至少包含一个矢量路径，并且不含脚本、嵌入位图或外部引用。任一条件失败时只清理当前临时批次，不保留半成品。

## 后续扩展

1. 把 DXF plan 接入 KORYAO 核心 server，等待核心分支合并后再注册。
2. 增加更多实体：arc、dimension、mtext、hatch。
3. 增加 AutoCAD COM 打开 DXF 的可选验证，但默认关闭。
4. 研究 AutoCAD LT File IPC，不直接依赖窗口焦点。
5. 增加 DWG 打开验证前的人为确认流程。
