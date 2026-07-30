# CAD / DXF Bridge 原型

这个目录是 KORYAO 的安全 CAD / DXF bridge 原型示例。它先做无头 DXF plan 验证和 dry-run 汇总，不直接控制真实 AutoCAD，不打开 DWG，不写客户项目文件。

## 当前能力

- 读取公开安全的 `example_plan.json`。
- 调用 `validate_cad_plan` 验证结构。
- 调用 `summarize_plan` 统计图层和实体数量。
- 默认调用 `write_dxf(..., dry_run=True)`，不会写 DXF 文件。
- 明确确认真实写入后，先比较批准 plan 与回读 DXF 的单位、图层、几何和文字内容指纹，再同批次生成 DXF、保留实体图层元数据的白底黑线 SVG 预览和产物清单。

## 是否需要 AutoCAD

不需要。当前原型不调用真实 AutoCAD，也不要求本机安装 AutoCAD。

## 可选依赖

如果本机安装了 `ezdxf`，可以在后续显式设置 `dry_run=False`，并且只允许写到：

```text
examples/cad/output/
```

不要把真实 DXF、SVG 预览或 manifest 输出提交到 Git。`output/` 只保留 `.gitkeep`。

## 运行

```powershell
python examples\cad\generate_dxf_plan.py
```

输出是统一 JSON，包含 validate、summary 和 dry-run write 结果。
