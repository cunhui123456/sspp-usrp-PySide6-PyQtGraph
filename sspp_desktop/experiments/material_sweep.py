"""材料扫频的研究问题与条件命名；采集仍委托统一控制器。"""
from PySide6 import QtWidgets


class MaterialSweepWidget(QtWidgets.QGroupBox):
    def __init__(self, parent=None):
        super().__init__("材料扫频 · 当前样品", parent)
        layout = QtWidgets.QFormLayout(self)
        self.material_name = QtWidgets.QLineEdit()
        self.material_name.setMaxLength(80)
        self.material_name.setPlaceholderText("必填，例如 空瓶、水瓶_2of3")
        layout.addRow("材料／条件名称 *", self.material_name)
        self.instructions = QtWidgets.QLabel(
            "固定样品与线缆 → 填写条件 → 自动逐点扫频 → 比较各轮幅度。\n"
            "名称作为本次实验标签保存；更换材料或状态后另起一次实验。")
        self.instructions.setWordWrap(True)
        self.instructions.setObjectName("muted")
        layout.addRow(self.instructions)

    def name(self):
        return self.material_name.text().strip()

    def validate(self):
        if not self.name():
            raise ValueError("请填写材料／条件名称，例如‘空瓶’或‘水瓶_2of3’。")
