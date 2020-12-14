# _*_ coding: utf-8 _*_

import re
import os

try:
    from PyQt5.QtGui import *
    from PyQt5.QtCore import *
    from PyQt5.QtWidgets import *
except ImportError:
    from PyQt4.QtGui import *
    from PyQt4.QtCore import *

from common.defectSelection.positionButton import PositionButton
from common.defectSelection.defectButton import DefectButton

from config import config
import yaml
from common.ui.settings import QDialogWithFontSettings

from common.document import el_doc, wg_doc


class DefectSelectionDialog(QDialog):
    def __init__(self, parent, controller, vi_item, el_or_wg, title="缺陷详情确认"):
        super(DefectSelectionDialog, self).__init__(parent)
        self.doc = el_doc if el_or_wg == 'el' else wg_doc
        self.flip_row_order = False
        self.flip_col_order = False
        if vi_item:
            self.flip_row_order = vi_item.el_data.img_info.ext_info.get('flip_row_order', False)
            self.flip_col_order = vi_item.el_data.img_info.ext_info.get('flip_col_order', False)
        self._controller = controller
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() & ~
                            Qt.WindowContextHelpButtonHint |
                            Qt.CustomizeWindowHint |
                            Qt.WindowMinMaxButtonsHint)
        self.accepted.connect(self.onAccepted)
        self.rejected.connect(self.onRejected)
        self._setupUI(el_or_wg)
    
    def _setupUI(self, el_or_wg):
        self._defect_buttons = []
        self._position_buttons = []
        self._defect_infos_label = None
        self.font_size = config.get_config("confirm_station.defect_selection.font_size", 20)
        style = "background-color:#C0C0C0; font-family:'黑体'; font-size:%dpx;" % self.font_size

        self.setStyleSheet("background-color:#FFFFFF")
        mailayout = QVBoxLayout()
        self.setLayout(mailayout)
        posWidget = QWidget()
        posLayout = QGridLayout()
        posWidget.setLayout(posLayout)

        defectWidget = QWidget()
        defectWidget.setStyleSheet("background-color:#D0D0D0")
        defectLayout = QGridLayout()
        defectWidget.setLayout(defectLayout)

        confirmWidget = QWidget()
        confirmLayout = QGridLayout()
        confirmWidget.setLayout(confirmLayout)

        mailayout.addWidget(posWidget)
        mailayout.addWidget(defectWidget)
        mailayout.addWidget(confirmWidget)

        # position buttons
        row_num = config.get_config('camera_station.image_source.rows', 6)
        col_num = config.get_config('camera_station.image_source.cols', 12)
        position_button_width = config.get_config("confirm_station.defect_selection.position_button_width", 40)
        position_button_height = config.get_config("confirm_station.defect_selection.position_button_height", 80)
        self.row_labels = config.get_config('mes.row_labels', 'ABCDEF')
        if self.flip_row_order:
            self.row_labels = ''.join(reversed(self.row_labels))

        pos_btn_positions = [(i, j) for i in range(row_num) for j in range(col_num)]

        for position in pos_btn_positions:
            name_button = '{}{}'.format(self.row_labels[position[0]], col_num - position[1] if self.flip_col_order else position[1] + 1)
            posButton = PositionButton(self, name_button, position_button_width, position_button_height)
            self._position_buttons.append(posButton)
            posLayout.addWidget(posButton, *position)

        # defect buttons
        defect_row_num = config.get_config("confirm_station.defect_selection.el_defects_row_num", 3)
        defect_col_num = config.get_config("confirm_station.defect_selection.el_defects_col_num", 6)
        defect_button_width = config.get_config("confirm_station.defect_selection.defect_button_width", 140)
        defect_button_height = config.get_config("confirm_station.defect_selection.defect_button_height", 30)
        defect_btn_positions = [(i,j) for i in range(defect_row_num) for j in range(defect_col_num)]

        el_defect_labels = self.el_defect_labels(el_or_wg)
        for index, position in enumerate(defect_btn_positions):
            defectButton = DefectButton(self, None, defect_button_width, defect_button_height)
            enabled = False
            if index < len(el_defect_labels):
                defectButton.setText(el_defect_labels[index])
                enabled = True
                self._defect_buttons.append(defectButton)

            defectButton.setEnabled(enabled)
            defectLayout.addWidget(defectButton, *position)

        # info area and reset/cancel/ok button
        infoLayout = QHBoxLayout()
        confirmLayout.addLayout(infoLayout, 0, 0, 1, 3)

        # defect infos
        self._defect_infos_label = QLabel()
        self._defect_infos_label.setWordWrap(True)
        infoLayout.addWidget(self._defect_infos_label)

        # reset/cancel/ok
        reset_button = QPushButton('重置')
        reset_button.setFixedSize(defect_button_width * 0.8, position_button_width)
        reset_button.setStyleSheet(style)

        confirmLayout.addWidget(reset_button, 0, 3)
        reset_button.clicked.connect(self.on_reset)

        close_button = QPushButton('关闭')
        close_button.setFixedSize(defect_button_width * 1.1, position_button_width)
        close_button.setStyleSheet(style)
        confirmLayout.addWidget(close_button, 0, 4)
        close_button.clicked.connect(self.on_close)

        ok_button = QPushButton('确定')
        ok_button.setFixedSize(defect_button_width * 1.1, position_button_width)
        ok_button.setStyleSheet(style)
        confirmLayout.addWidget(ok_button, 0, 5)
        ok_button.clicked.connect(self.on_ok)

    def popUp(self):
        self._controller.on_reset()
        positions = self._controller.current_defect_positions()
        defects = self._controller.valid_defects()
        self.update_defect_buttons(defects)
        self.update_position_buttons(positions)
        self.update_defect_infos()
        self.exec_()
   
    def defectButton(self, text):
        for button in self._defect_buttons:
            if button.text() == text:
                return button
        return None

    def positionButton(self, text):
        for button in self._position_buttons:
            if button.text() == text:
                return button
        return None

    def onDefectButtonClicked(self, defect):
        self._controller.set_current_defect(defect)
        positions = self._controller.current_defect_positions()
        defects = self._controller.valid_defects()
        self.update_defect_buttons(defects)
        self.update_position_buttons(positions)
        self.update_defect_infos()

    def onPositionButtonClicked(self, pos):
        self._controller.turn_defect_position(pos)
        positions = self._controller.current_defect_positions()
        self.update_position_buttons(positions)
        self.update_defect_infos()

    def on_reset(self):
        current_defect = self._controller.current_defect()
        self._controller.on_reset()
        self.onDefectButtonClicked(current_defect)

    def on_close(self):
        if config.get_config("confirm_station.defect_selection.confirm_close", True):
            msbox = QMessageBox(QMessageBox.Warning, "提示",
                                "您确定取消所有选中的缺陷并退出吗？")
            center_pos = QDesktopWidget().availableGeometry().center()
            msbox.move(center_pos.x(), center_pos.y())
            no = msbox.addButton("否", QMessageBox.NoRole)
            yes = msbox.addButton("是", QMessageBox.YesRole)
            msbox.setDefaultButton(no)
            msbox.exec_()
            if msbox.clickedButton() == no:
                return

        self._controller.on_close() 
        self.reject()
    
    def on_ok(self):
        infos = self.defect_position_infos()
        center_pos = QDesktopWidget().availableGeometry().center()
        if not infos:
            msbox = QMessageBox(QMessageBox.Warning, "提示",
                                "您还没选择相关缺陷")

            msbox.move(center_pos.x(), center_pos.y())
            no = msbox.addButton("重新选择", QMessageBox.NoRole)
            msbox.setDefaultButton(no)
            msbox.exec_()
            if msbox.clickedButton() == no:
                return
        elif not config.get_config("confirm_station.defect_selection.confirm_ok", True):
            self._controller.on_ok()
            self.accept()
        if config.get_config("confirm_station.defect_selection.confirm_ok", True):
            msbox = QMessageBox(QMessageBox.Warning, "提示",
                                "您确定已经完成缺陷选择并退出吗？")
            msbox.move(center_pos.x(), center_pos.y())
            no = msbox.addButton("否", QMessageBox.NoRole)
            yes = msbox.addButton("是", QMessageBox.YesRole)
            msbox.setDefaultButton(no)
            msbox.exec_()
            if msbox.clickedButton() == no:
                return
            else:
                self._controller.on_ok()
                self.accept()
    
    def update_defect_buttons(self, valid_defects):
        for button in self._defect_buttons:
            button.set_valid(button.text() in valid_defects)
            button.setChecked(False)
        if self._controller.current_defect():
            self.defectButton(self._controller.current_defect()).setChecked(True)

    def update_position_buttons(self, valid_positions):
      for button in self._position_buttons:
            button.set_valid(button.text() in valid_positions)
    
    def update_defect_infos(self):
        infos = self.defect_position_infos()
        str_info = ''
        for info in infos:
            str_info += info + ' '
        
        self._defect_infos_label.setText(str_info)
    
    def defect_position_infos(self):
        return self._controller.defect_position_infos()
    
    def el_defect_labels(self, el_or_wg):
        return self._controller.el_defect_labels(el_or_wg)

    def onAccepted(self):
        pass

    def onRejected(self):
        self._controller.on_reset()