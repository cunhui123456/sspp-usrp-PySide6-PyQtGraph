"""独立 Agg 子进程绘制比较报告，避免与 Qt 绘图线程争用。"""
import json
from pathlib import Path
import sys


def export_figures(report, directory):
    import numpy as np
    from matplotlib import font_manager
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure
    directory = Path(directory)
    font = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    font_properties = font_manager.FontProperties(fname=str(font)) if font.is_file() else None
    charts = report.get("charts", [])
    if not charts:
        raise ValueError("没有可绘制的对比数据")
    with PdfPages(directory / "comparison_report.pdf") as pdf:
        for index, chart in enumerate(charts, 1):
            fig = Figure(figsize=(12, 7.5), layout="constrained")
            FigureCanvasAgg(fig)
            ax = fig.subplots()
            for series in chart.get("series", []):
                data = np.asarray(series.get("data", []), dtype=float)
                if not data.size:
                    continue
                x, y = data[:, 0], data[:, 1]
                ax.plot(x, y, label=series["label"], color=series.get("color"), linewidth=1.2)
                errors = series.get("errors")
                if errors is not None and len(errors) == len(x):
                    error = np.asarray(errors, dtype=float)
                    ax.errorbar(x, y, yerr=error, fmt="none", ecolor=series.get("color"), capsize=3, alpha=.65)
            for line in chart.get("horizontal", []):
                ax.axhline(line["y"], color="#81909a", linewidth=.8, linestyle="--")
            ax.set_title(chart["title"], fontproperties=font_properties, loc="left")
            source = next((s for s in report.get("sources", []) if s["key"] == report.get("reference_key")), {})
            if report.get("kind") in ("material_sweep", "liquid_temperature_sweep"):
                heading = f"参考：{source.get('label', '')} · {report.get('reference_key', '')}  |  差值 = 目标 − 参考"
            elif report.get("kind") == "liquid_temperature_phase":
                heading = "各温度平台使用各自run内的参考位置；只比较相对响应，远离为正"
            else:
                heading = "各实验使用自己的静止参考；参考结束为时间零点，不代表动作同步"
            fig.suptitle(heading, fontproperties=font_properties, fontsize=9, color="#586c79")
            ax.set_xlabel(chart["xlabel"], fontproperties=font_properties)
            ax.set_ylabel(chart["ylabel"], fontproperties=font_properties)
            ax.grid(alpha=.2)
            handles, labels = ax.get_legend_handles_labels()
            if handles:
                ax.legend(handles, labels, prop=font_properties, fontsize=8, ncols=2)
            import textwrap
            note = chart.get("note", "")
            fig.text(.02, .015, "\n".join(textwrap.wrap(note, 78)), fontproperties=font_properties,
                     fontsize=8, color="#586c79", va="bottom")
            fig.get_layout_engine().set(rect=(0, .09, 1, .91))
            fig.savefig(directory / f"comparison_{index:02d}_{chart['id']}.png", dpi=170)
            pdf.savefig(fig)
            fig.clear()


if __name__ == "__main__":
    export_figures(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
