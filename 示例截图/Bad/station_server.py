'''机台端复检， 接受el检测结果'''
import io
import logging
import json
import os
import shutil
import threading
import datetime
import random
import os.path as osp
import time
import copy

from wsgiref.simple_server import make_server
from gevent.pywsgi import WSGIServer

from PyQt5.QtCore import QPointF, pyqtSignal, QObject
from flask import Flask, request
import requests
from pandas import DataFrame
import cv2

import functools
from cacheout import Cache
from config import config, status, site
from el_lib.el_data import ELData, VIResult, VIData
from el_lib import errcode
from common.io_44 import io_44
from el_lib.json_coders import HPJsonDecoder, HPJsonEncoder
from el_lib.hp_utils import img_bytes_2_cv, img_cv_2_bytes
from common.ui.shape import DefectShape
from common.pascal_voc_io import PascalVocWriter
from common.warning import WarningDefects
from el_lib.el_data import VIItem
from app import get_app
from common.mes_client import MesClient
from common.save_module_pic import save_mes_pic
from common.document import el_doc
from common.visual_key_sender import send_result


class Result(QObject):
    rpa_send_result_signal = pyqtSignal(str)


class StationServer(threading.Thread):
    '''机台端复检， 接受el检测结果'''
    result = Result()

    def __init__(self):
        super().__init__()
        self.setDaemon(True)
        self.result_dict = {}  # 保存 vi_result 接口获取的结果
        self.defect_dict = {}  # 保存 vi_result 图片缺陷坐标
        self.io44 = io_44
        self.sanner_code = 0
        self.repair_strings = 0
        self.timer_ttl = 300  # 定时器超时时间
        self.module_ttl = 30  # 组件超时时间
        self.cache = Cache(maxsize=4096, ttl=0, timer=time.time, default=None)
        self.waring = WarningDefects()
        self.defect_label_map = self._get_defect_label_map()

    def flask_app(self):
        app = Flask(__name__)
        app.add_url_rule('/vi_result', endpoint='vi_result',
                         view_func=self.vi_result, methods=('POST',))
        app.add_url_rule('/get_vi_result', endpoint='get_vi_result',
                         view_func=self.get_vi_result, methods=('POST',))
        app.add_url_rule('/stop_warning', endpoint='stop_warning',
                         view_func=self.stop_warning, methods=('GET',))
        app.add_url_rule('/stop_voice', endpoint='stop_voice',
                         view_func=self.stop_voice, methods=('GET',))
        app.add_url_rule('/check_timeout', endpoint='check_timeout',
                         view_func=self.check_timeout, methods=('POST',))
        app.add_url_rule('/echo/<string:msg>', endpoint='echo',
                         view_func=self.echo, methods=('GET',))
        return app

    def run(self):
        '''start server'''
        station_server = config.get_config('servers.station_server', '')
        logging.info('start station server at %r', station_server)
        port = int(station_server.split(':')[1])

        # http_server = WSGIServer(('0.0.0.0', port), self.app)
        self.http_server = make_server('0.0.0.0', port, self.flask_app())
        self.http_server.serve_forever()

    def echo(self, msg):
        return msg

    def commit_to_mes(self, el_data, vi_result):
        """根据配置判断是否提交结果到mes"""
        if config.get_config('mes.enabled', False):
            MesClient().send_result_to_mes(el_data, vi_result)
            module_id = el_data.module_info.id
            if module_id:
                self.cache.set(module_id, True, ttl=self.module_ttl)

    def determine_commit_to_mes(self, el_data, vi_data, vi_result, confirmed_result):
        """根据缓存时间及结果决定是否上传mes"""
        module_id = el_data.module_info.id
        if self.cache.get(module_id):
            logging.info('组件:%s在规定时间内已提交', module_id)
        else:
            logging.warning('组件:%s在规定时间内未提交,将自动上传AI结果！！', module_id)
            self._vi_result(el_data, vi_data, vi_result, confirmed_result, auto_commit=True)

    def check_timeout(self):
        """检查人工复检是否超时，超时则自动将AI检测结果提交mes"""
        data = request.get_data()
        json_data = json.loads(data.decode("utf-8"), cls=HPJsonDecoder)
        el_data = json_data['el_data']
        vi_data = json_data['vi_data']
        vi_result = json_data['vi_result']
        confirmed_result = json_data['confirmed_result']
        logging.info('开始VI结果输出 %s', el_data.img_info.img_name)

        auto_commit_time = config.get_config('mes.auto_commit_time', 15)
        auto_commit_time = int(auto_commit_time) if auto_commit_time else 15
        # 设置自动提交mes的时间
        timerCallback = functools.partial(self.determine_commit_to_mes, el_data, vi_data, vi_result, confirmed_result)
        timer = threading.Timer(auto_commit_time, timerCallback)  # 停留设定好的时间再去调用 fun_timer
        timer.setDaemon(True)
        self.cache.set(time.time(), timer, ttl=self.timer_ttl)
        timer.start()
        return json.dumps({"success": True}, cls=HPJsonEncoder)

    def cengqian_repair_ai(self, el_data, vi_result, vi_data):
        # 层前返修AI复检判断
        if config.get_config('cengqian_repair.check_repeat', False) and vi_data.confirm_mode == 'none':
            logging.info('去重判断开始')
            module_id = el_data.module_info.id
            querys = {
                'facility': el_data.mes_info.facility_id,
                'product_line': el_data.mes_info.product_line,
                'stage': el_data.module_info.station_stage,
                'equip_id': el_data.mes_info.equip_id,
                'module_id': module_id,
            }
            df = DataFrame(vi_result.to_dict()['defects'])
            df.rename(columns={'class': 'display_name', 'prob': 'confidence'}, inplace=True)
            df.sort_values(['display_name', 'confidence'], inplace=True)
            df.confidence = df.confidence.round(6)
            new_result = df.to_dict(orient='records')
            cq_host, cq_port = config.get_config('servers.el_platform', '127.0.0.1:8001').split(':')
            logging.info('查询层前AI历史检测记录')
            try:
                resp = requests.get('http://{}:{}/api/cengqian/defects/'.format(cq_host, cq_port), params=querys,
                                    timeout=10)
            except Exception as e:
                logging.exception(e)
                logging.info('%s层前历史记录查询失败' % module_id)
            else:
                if resp.status_code == 200:
                    history_resp = DataFrame(resp.json()['data']). \
                        sort_values(['display_name', 'confidence']).to_dict(orient='records')
                    if history_resp == new_result:
                        vi_result.is_ng = False
                        logging.info('%s:层前检测判断为过检 ng=false' % module_id)
                    else:
                        logging.info('%s: 检测判断为新缺陷 ng=true' % module_id)
                elif resp.status_code == 404:
                    logging.info('cengqian 检测查询无NG记录 %s,ng = true' % module_id)

        if vi_result.is_ng:
            self.cengqian_image_upload(el_data, vi_result, )
        return vi_result.is_ng

    def delete_cengqian_repair(self, el_data):
        if site.get_config('identity.stage', False) == 'cengqian' and config.get_config('cengqian_repair.enabled',
                                                                                        False):
            cq_host, cq_port = config.get_config('servers.el_platform', '127.0.0.1:8001').split(':')
            try:
                delete_resp = requests.delete(
                    'http://{}:{}/api/cengqian/ng_image/{}'.format(cq_host, cq_port, el_data.module_info.id),
                    timeout=5).json()
            except Exception as e:
                logging.exception(e)
                logging.info('{}ng图片删除失败'.format(el_data.module_info.id))
            else:
                logging.info(delete_resp)

    def _get_defect_label_map(self):
        el_defects = config.get_config("el_defects_jingao", [])
        defect_label_map = dict()
        for item in el_defects:
            defect_label_map[item.get("code")] = item.get("label")
        return defect_label_map

    def vi_config_to_json(self,el_data,vi_result):
        try:
            LocationMap = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}
            trans_data = {'items': []}
            y_point = el_data.img_info.row_lines
            x_point = el_data.img_info.col_lines
            if not vi_result.get('defect_selected',None):
                return json.dumps({'trans_data': trans_data})
            for defect, defect_detail in vi_result['defect_selected'].items():
                for location_detail in defect_detail:
                    location_detail = str(location_detail)
                    item = {
                        'location': '{}X{}'.format(LocationMap.get(location_detail[0], 0),
                                                   location_detail[1:] if len(location_detail) >= 2 else 0),
                        'reasonDetail': self.defect_label_map.get(defect),
                    }
                    trans_data['items'].append(item)
            res = {'trans_data': trans_data,'x_point':x_point,'y_point':y_point}
            return json.dumps(res)
        except Exception as e:
            logging.exception(e)
            return json.dumps({'trans_data': trans_data})

    def cengqian_image_upload(self, el_data, vi_result=None, vi_config={}):
        # 图片上传
        cq_host, cq_port = config.get_config('servers.el_platform', '127.0.0.1:8001').split(':')
        el_data_copy = copy.deepcopy(el_data)
        _img = el_data_copy.img_info.img_cv2
        buf = io.BytesIO()
        if vi_result:
            if 'x_point' not in vi_config:
                for defect in vi_result.defects:
                    left_top, right_botton = tuple(defect['coord'][:2]), tuple(defect['coord'][2:])
                    # 图片缺陷标注
                    cv2.rectangle(_img, left_top, right_botton, (0, 0, 255), thickness=2)
        buf.write(img_cv_2_bytes(_img))
        buf.seek(0)
        post_data = {
            'module_id': el_data.module_info.id,
            'img_name': el_data.img_info.img_name,
            'vi_defect': vi_config
        }
        try:
            upload_resp = requests.post('http://{}:{}/api/cengqian/ng_image/'.format(cq_host, cq_port),
                                        data=post_data,
                                        files={'img': buf}, timeout=10)
        except Exception as e:
            logging.exception(e)
            logging.info('层前NG {} 图片上传失败'.format(el_data.module_info.id))
        else:
            del buf, el_data_copy
            logging.info('层前NG img上传成功 {}'.format(el_data.module_info.id))


    def vi_result(self):
        """missing docstring"""
        data = request.get_data()
        json_data = json.loads(data.decode("utf-8"), cls=HPJsonDecoder)
        el_data = json_data['el_data']
        vi_data = json_data['vi_data']
        vi_result = json_data['vi_result']
        confirmed_result = json_data['confirmed_result']
        logging.info('开始VI结果输出 %s', el_data.img_info.img_name)
        return self._vi_result(el_data, vi_data, vi_result, confirmed_result, auto_commit=False)

    def _vi_result(self, el_data, vi_data, vi_result, confirmed_result, auto_commit=True):
        """missing docstring"""
        rv = {}
        flag = 'ok'

        if site.get_config('identity.stage', False) == 'chuanjian' and vi_result is None:
            print("chuangjian: vi_result is None")
            vi_result = VIResult()

        # 层前复检判断
        if site.get_config('identity.stage', False) == 'cengqian' and config.get_config('cengqian_repair.enabled',False):

            logging.info('层前 复检判断开始')
            try:
                if confirmed_result:
                    # 手工检测
                    confirmed_flag = confirmed_result.ext_info.get('vi_confirm_ng', None)
                    if confirmed_flag and confirmed_result.is_ng:
                        # confirmed_result.is_ng = self.cengqian_repair(confirmed_result, el_data,vi_result)
                        vi_config = self.vi_config_to_json(el_data,confirmed_result.ext_info)
                        self.cengqian_image_upload(el_data, vi_result=vi_result,vi_config=vi_config)
                if not confirmed_result or confirmed_result.ext_info.get('vi_confirm_ng', None) is None:
                    # AI检测
                    if vi_result.is_ng:
                        vi_result.is_ng = self.cengqian_repair_ai(el_data, vi_result,vi_data)
                        confirmed_result.is_ng = vi_result.is_ng
            except Exception as e:
                logging.exception(e)

        if confirmed_result:
            logging.info(config.get_config('camera_station.confirm_mode', None))
            if site.get_config('identity.stage', False) == 'cenghou' and \
                    config.get_config('camera_station.confirm_mode', None) != "none":
                if config.get_config('camera_station.confirm_mode', None) == "ng_only":
                    if vi_result:
                        if vi_result.is_ng is True:
                            flag = confirmed_result.ext_info.get('lotGrade', None)
                        else:
                            flag = 'ng' if vi_result.is_ng else 'ok'
                else:
                    flag = confirmed_result.ext_info.get('lotGrade', None)
            else:
                flag = 'ng' if confirmed_result.is_ng else 'ok'
        elif vi_result:
            flag = 'ng' if vi_result.is_ng else 'ok'

        # 层前ng 图片删除
        if flag == 'ok':
            self.delete_cengqian_repair(el_data,)

        img_name = el_data.img_info.img_name
        img_id = img_name.split('.')[0]
        defects_coord = None
        if vi_result and vi_result.is_ng:
            defects_coord = vi_result.defects
        # 判断是否报警
        self.waring.run(flag, vi_result.defects)

        self.result_dict[img_id] = flag
        self.defect_dict[img_id] = defects_coord

        if self.io44.is_valid():
            if flag == "ok":
                logging.info("io开始发送OK信号")
                self.io44.set_el_ok()
            else:
                logging.info("io开始发送NG信号")
                self.io44.set_el_ng()

        # 为mes保存整图
        save_mes_pic(el_data)

        # 提交mes, 如果自动提交提交vi_result, 如果复核提交,提交confirmed_result
        if auto_commit:
            logging.info("判断为自动提交mes模式")
            self.commit_to_mes(el_data, vi_result)
        else:
            logging.info("判断为复核提交mes模式")
            self.commit_to_mes(el_data, confirmed_result)
        # 发送检测结果
        app = get_app()
        if hasattr(app, 'station_server'):
            logging.info('rpa signal emitted: %s', flag.upper())
            self.result.rpa_send_result_signal.emit(flag.upper())

        app.opt.send_result(flag)

        # 发送结果到中台数据库
        self.send_resut_to_db(el_data, vi_data, vi_result, confirmed_result)

        # 保持检测结果到本地用于内部分析
        vi_item = VIItem(el_data, vi_data=vi_data, vi_result=vi_result, confirmed_result=confirmed_result)
        threading.Thread(target=self.save_results, args=(vi_item, ), daemon=True).start()  # 创建线程

        # string_rapir_full = False
        # 复检台返修功能，将ng图片上传中台以供返修台使用
        if config.get_config('string_repair.enabled', False):
            el_data_4_repair = copy.deepcopy(el_data)
            if app.scanner:
                if not app.scanner.current_bar_code:
                    logging.error("返修盒没有扫码，请扫码")
                    return json.dumps(rv, cls=HPJsonEncoder)
            threading.Thread(target=self.save_string_repair_2_hub, args=(app, el_data_4_repair, vi_result, flag), daemon=True).start()  # 创建线程

        logging.info('完成VI结果输出 %s', el_data.img_info.img_name)
        return json.dumps(rv, cls=HPJsonEncoder)

    def save_string_repair_2_hub(self, app, el_data_4_repair, vi_result, flag):
        """保存ng图片到hub"""
        sleep_for_repair = config.get_config("string_repair.sleep_4_save_string_repair", 5)
        time.sleep(sleep_for_repair)

        if app.scanner.current_bar_code:
            if self.sanner_code != app.scanner.current_bar_code:
                self.repair_strings = 0
                self.sanner_code = app.scanner.current_bar_code
                el_doc.set_err(errcode.RepairFull, False)
                self.io44.set_repair_full(False)
        else:
            self.repair_strings = 0

        repair_full_num = config.get_config("camera_station.repair_full", 0)
        if repair_full_num:
            if flag == 'ng':
                self.repair_strings = self.repair_strings + 1
            if self.repair_strings >= repair_full_num:
                el_doc.set_err(errcode.RepairFull, True)
                self.io44.set_repair_full(True)
            else:
                el_doc.set_err(errcode.RepairFull, False)
                self.io44.set_repair_full(False)

        if flag == "ok":
            return
        el_data_4_repair.module_info.id = app.scanner.current_bar_code

        headers = {'content-type': "application/json"}
        # 在左下角给图片上加上机台信息
        img = el_data_4_repair.img_info.img_cv2
        BLACK = [0, 0, 0]
        margin = config.get_config("string_repair.string_margin_tag", 0)
        if margin:
            img = cv2.copyMakeBorder(img, 0, 80, 0, 0, cv2.BORDER_CONSTANT, value=BLACK)
        txt = '{} {} {}'.format(el_data_4_repair.module_info.id, el_data_4_repair.mes_info.equip_id, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        font = cv2.FONT_HERSHEY_SIMPLEX
        txt_tl = (20, img.shape[0]-20)
        cv2.putText(img, txt, txt_tl, font, 1, (255, 255, 255), lineType=cv2.LINE_AA)
        el_data_4_repair.img_info.img_cv2 = img

        post_data = {
            'el_data': el_data_4_repair,
            'vi_result': vi_result
        }
        host, port = config.get_config(
            'servers.platform', '127.0.0.1:8001').split(':')
        url = 'http://{}:{}/record_chuan_ng'.format(host, port)
        logging.info('发送串返修数据到中台')
        data = json.dumps(post_data, cls=HPJsonEncoder)
        try:
            response = requests.post(url, data=data, headers=headers, timeout=10)
        except Exception as e:
            logging.error(e)
            logging.info('发送串返修数据到中台失败')

        logging.info('发送串返修数据到中台 %s', '成功' if response.status_code == 200 else '失败')

    def get_vi_result(self):
        '''WEB服务接口，通过 图片ID ，获取对应图片的检测结果
        输入：带 data(img_id) 的POST提交的request
        输出：对应的检测结果 或 None
        '''
        try:
            self.local_time = time.time()
            data = request.get_data()
            json_data = json.loads(data.decode(), cls=HPJsonDecoder)
            img_id = json_data.get('img_id')  # 获取检测图片的id
            vi_result = self.result_dict.get(img_id)
            defect_coord = self.defect_dict.get(img_id)
            result = {'vi_result': vi_result, 'defect_coord': defect_coord}
            logging.info("检测结果", result)
        except Exception:
            result = None
        return json.dumps(result, cls=HPJsonEncoder)

    def stop_warning(self):
        "获取是否报警"
        self.waring.clean_data()
        return 'SUCCESS'

    def stop_voice(self):
        "获取是否报警"
        self.io44.stop_voice()
        return 'SUCCESS'

    def send_resut_to_db(self, el_data, vi_data, vi_result, confirmed_result):
        """missing docstring"""
        try:
            # 保存结果复检结果到中台
            host, port = vi_data.platform.split(':')
            tmp = el_data.img_info.img_bytes
            el_data.img_info.img_bytes = b''
            post_data = {
                'el_data': el_data,
                'vi_data': vi_data,
                'vi_result': vi_result,
                'confirmed_result': confirmed_result
            }
            data = json.dumps(post_data, cls=HPJsonEncoder)
            el_data.img_info.img_bytes = tmp

            headers = {'content-type': "application/json"}
            url = 'http://{}:{}/save_vi_result'.format(host, port)
            logging.info('send the vi confirm result to %s', url)
            response = requests.post(url, data=data, headers=headers, timeout=3)
            if response.status_code == 200:
                logging.info('发送结果到数据库成功')
        except requests.exceptions.RequestException:
            logging.exception("发送结果到数据库失败")
        except Exception:
            logging.exception("发送结果到数据库失败")

    def save_results(self, vi_item):
        """missing docstring"""
        try:
            logging.info('保存当前检测结果 for %s',
                         vi_item.el_data.img_info.img_name)
            if vi_item.el_data.img_info.img_class == "el":
                save_dir = config.get_config('camera_station.el_result_dir', '')
            else:
                save_dir = config.get_config('wg_camera_station.wg_saving_dir', '')
            self.save_vi_data(vi_item, save_dir, vi_item.el_data.img_info.img_class)
            self.save_accuracy_data(vi_item, save_dir)

        except Exception:
            logging.exception('save result failed')

    def save_vi_data(self, vi_item, save_dir, img_class):

        """missing docstring"""
        logging.info('save_vi_data')
        if not save_dir:
            return

        img_name = os.path.basename(vi_item.el_data.img_info.img_name)
        img_names = os.path.splitext(img_name)

        # img_info = vi_item.el_data.img_info

        shift = vi_item.el_data.module_info.shift
        shift_date = shift.split('-')[0]
        a_b_dir = img_names[0][-1]  # 取图片的后一位
        if config.get_config('camera_station.el_result_A/B_dir', False):
            shift_date = os.path.join(shift_date, a_b_dir)

        station_type = 'camera_station' if img_class == 'el' else 'wg_camera_station'

        # 保存ai结果
        csv_path = os.path.join(save_dir, shift_date, '全部结果_AI.csv')

        if not os.path.exists(os.path.dirname(csv_path)):
            os.makedirs(os.path.dirname(csv_path))

        if not os.path.exists(csv_path):
            head = ['序列号', '过检时间', '结果', '缺陷信息']
            df_data = DataFrame([], columns=head)
            df_data.to_csv(csv_path, encoding="utf_8_sig", index=False)

        strtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if vi_item.vi_result is None:
            df_data = DataFrame([[img_names[0], strtime, 'UNKNOWN', '/']])
            df_data.to_csv(csv_path, mode='a',
                           encoding="utf_8_sig", index=False, header=False)

        elif not vi_item.vi_result.is_ng:

            # 经过配方为OK的，但是AI实际上检测出缺陷的图片需要保存
            if len(vi_item.vi_result.defects) > 0 and config.get_config("camera_station.result_save.save_ok_with_box", 0):
                logging.info("配方后OK，实际检测出缺陷：%s个" % len(vi_item.vi_result.defects))
                dir_path = os.path.join(save_dir, shift_date, "ok_with_box")
                if not os.path.exists(dir_path):
                    os.makedirs(dir_path)
                self.save_ai_ng_with_box_img(vi_item=vi_item, dir_path=dir_path, img_names=img_names, img_name=img_name,
                                             str_time=strtime, csv_path=csv_path)
            else:
                df_data = DataFrame([[img_names[0], strtime, 'OK', '/']])
                df_data.to_csv(csv_path, mode='a',
                               encoding="utf_8_sig", index=False, header=False)

                save_ok = config.get_config(station_type + '.result_save.save_ok', 0)
                if random.random() < save_ok and vi_item.img_bytes:
                    logging.warning('  #保存aiok图片 %s', vi_item.el_data.img_info.img_name)
                    img_path = os.path.join(save_dir, img_name)
                    target_path = os.path.join(save_dir, shift_date, 'ok', img_name)
                    if not os.path.exists(os.path.dirname(target_path)):
                        os.makedirs(os.path.dirname(target_path))
                    if os.path.exists(img_path):
                        shutil.move(img_path, target_path)
                    else:
                        with open(target_path, 'w+b') as fd:
                            fd.write(vi_item.el_data.img_info.img_bytes)
        else:
            details = []
            for vi_rv in vi_item.vi_result.defects:
                row, col = vi_rv['loc']
                details.append(str([vi_rv['class'], row, col, vi_rv['prob']]))
            details = ' | '.join(details)
            df_data = DataFrame([[img_names[0], strtime, 'NG', details]])
            df_data.to_csv(csv_path, mode='a',
                           encoding="utf_8_sig", index=False, header=False)

            # 保存带缺陷框的ng图片
            save_ng_with_box = config.get_config(
                station_type + '.result_save.save_ng_with_box', 0)

            def _lable_name2text(name):
                for xx in config.get_config(img_class+'_defects', []):
                    if xx['name'] == name:
                        return xx['text']
                return None
            if random.random() < save_ng_with_box and vi_item.img_bytes:
                dir_path = os.path.join(save_dir, shift_date, "ng_with_box")
                if not os.path.exists(dir_path):
                    os.makedirs(dir_path)
                img_path = os.path.join(dir_path, img_name)
                img_data = img_bytes_2_cv(vi_item.img_bytes)
                for vi_rv in vi_item.vi_result.defects:
                    txt = '{:.6f}'.format(vi_rv['prob'])
                    font = cv2.FONT_HERSHEY_SIMPLEX
                    minx, miny, maxx, maxy = vi_rv['coord']
                    offset = 15
                    minx = minx - offset
                    miny = miny - offset
                    maxx = maxx + offset
                    maxx = maxx + offset
                    txt_tl = (minx, miny) if miny > 50 else (minx, maxy + 20)
                    cv2.putText(img_data, txt, txt_tl, font, 1,
                                (0, 255, 255), lineType=cv2.LINE_AA)
                    shape = DefectShape(_lable_name2text(vi_rv['class']), [QPointF(0, 0), QPointF(1, 1)])
                    line_color = shape.line_color
                    cv2.rectangle(img_data, (minx, miny), (maxx, maxy),
                                  (line_color.blue(), line_color.green(), line_color.red()), thickness=2)

                cv2.imwrite(img_path, img_data)

            # 截取缺陷小图并保存
            save_ng_cell = config.get_config(
                station_type + '.result_save.save_ng_cell', 0)
            if random.random() < save_ng_cell and vi_item.img_bytes:
                dir_path = os.path.join(save_dir, shift_date, "ng_cell_img")
                el_data = vi_item.el_data
                img_data = img_bytes_2_cv(vi_item.img_bytes)
                # module_rows = el_data.module_info.rows
                # module_cols = el_data.module_info.cols
                half_plate = el_data.module_info.half_plate
                # cut_cols = module_cols//2 if half_plate else module_cols
                row_lines = el_data.img_info.row_lines
                col_lines = el_data.img_info.col_lines
                print("row_lines="+str(row_lines))

                for defect in vi_item.vi_result.defects:
                    if 'dummy' in defect['class']:
                        continue
                    row = defect['loc'][0]
                    col = defect['loc'][1]
                    if half_plate:
                        if (col % 2 ==0):
                            cell_x1 = col_lines[col-2]
                            cell_x2 = col_lines[col]
                        else:
                            cell_x1 = col_lines[col-1]
                            cell_x2 = col_lines[col+1]
                        col = (col + 1) // 2
                    else:
                        cell_x1 = col_lines[col - 1]
                        cell_x2 = col_lines[col]
                    cell_y1 = row_lines[row-1]
                    cell_y2 = row_lines[row]
                    cell_data = img_data[cell_y1:cell_y2, cell_x1:cell_x2, :]
                    cell_dir = osp.join(dir_path, defect['class'])

                    if not osp.exists(cell_dir):
                        os.makedirs(cell_dir)

                    img_basename, ext = osp.splitext(el_data.img_info.img_name)
                    cell_file = osp.join(
                        cell_dir, img_basename+'_{}_{}'.format(row, col)+ext)
                    cv2.imwrite(cell_file, cell_data)

            # 保存 缺陷的voc xml
            xml_dir = os.path.join(save_dir, shift_date, "voc_xmls")
            if not os.path.exists(xml_dir):
                os.mkdir(xml_dir)

            vocwriter = PascalVocWriter(foldername=xml_dir, filename=img_name, img_size=(
                0, 0), database_src='Unknown', local_img_path=xml_dir)

            for vi_rv in vi_item.vi_result.defects:
                minx, miny, maxx, maxy = vi_rv['coord']
                name = vi_rv['class']
                if 'dummy' in name:
                    continue
                if not name:
                    name = "unknown"
                for xx in config.get_config(img_class+'_defects', []):
                    if xx['text'] == name:
                        name = xx['name']
                        break
                vocwriter.add_bnd_box(minx, miny, maxx, maxy, name, False)
            xml_name = img_names[0] + ".xml"
            xml_path = os.path.join(xml_dir, xml_name)
            vocwriter.save(target_file=xml_path)

        # 保存复检结果
        csv_path = os.path.join(save_dir, shift_date, '全部结果.csv')
        if not os.path.exists(os.path.dirname(csv_path)):
            os.makedirs(os.path.dirname(csv_path))

        logging.info('save Canvas')

        if not os.path.exists(csv_path):
            head = ['序列号', '过检时间', '结果', '缺陷信息']
            df_data = DataFrame([], columns=head)
            df_data.to_csv(csv_path, encoding="utf_8_sig", index=False)

        strtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if vi_item.confirmed_result is None:
            df_data = DataFrame([[img_names[0], strtime, 'UNKNOWN', '/']])
            df_data.to_csv(csv_path, mode='a',
                           encoding="utf_8_sig", index=False, header=False)

        elif not vi_item.confirmed_result.is_ng:
            df_data = DataFrame([[img_names[0], strtime, 'OK', '/']])
            df_data.to_csv(csv_path, mode='a',
                           encoding="utf_8_sig", index=False, header=False)

        else:
            if site.get_config('identity.stage', False) == 'cenghou' and \
                    config.get_config('camera_station.confirm_mode', None) != "none":
                flag = vi_item.confirmed_result.ext_info.get("lotGrade", "ng")
            else:
                flag = 'ng'

            details = []
            for vi_rv in vi_item.confirmed_result.defects:
                row, col = vi_rv['loc']
                details.append(str([vi_rv['class'], row, col, vi_rv['prob']]))
            details = ' | '.join(details)
            df_data = DataFrame([[img_names[0], strtime, flag, details]])
            df_data.to_csv(csv_path, mode='a',
                           encoding="utf_8_sig", index=False, header=False)

        is_confirm_ng = True if vi_item.confirmed_result.is_ng else False

        save_ng = config.get_config(station_type + '.result_save.save_ng', 0)
        if random.random() < save_ng and is_confirm_ng and vi_item.img_bytes:

            if site.get_config('identity.stage', False) == 'cenghou' and \
                    config.get_config('camera_station.confirm_mode', None) != "none":
                flag = vi_item.confirmed_result.ext_info.get("lotGrade", "ng")
            else:
                flag = 'ng'

            logging.warning('发现%s图片 %s' % (flag, vi_item.el_data.img_info.img_name))
            # 保存人工ng 图片
            img_path = os.path.join(save_dir, img_name)
            target_path = os.path.join(save_dir, shift_date, flag, img_name)

            if not os.path.exists(os.path.dirname(target_path)):
                os.makedirs(os.path.dirname(target_path))
            if os.path.exists(img_path):
                shutil.move(img_path, target_path)
            else:
                with open(target_path, 'w+b') as fd:
                    fd.write(vi_item.el_data.img_info.img_bytes)

        save_ok = config.get_config(station_type + '.result_save.save_ok', 0)
        if random.random() < save_ok and is_confirm_ng is False and vi_item.img_bytes:
            logging.warning('发现ok图片 %s', vi_item.el_data.img_info.img_name)
            # 保存人工ok 图片
            img_path = os.path.join(save_dir, img_name)
            target_path = os.path.join(save_dir, shift_date, 'ok', img_name)

            if not os.path.exists(os.path.dirname(target_path)):
                os.makedirs(os.path.dirname(target_path))
            if os.path.exists(img_path):
                shutil.move(img_path, target_path)
            else:
                with open(target_path, 'w+b') as fd:
                    fd.write(vi_item.el_data.img_info.img_bytes)

    @staticmethod
    def save_accuracy_data(vi_item, save_dir):
        """missing docstring"""
        if not save_dir:
            return

        if vi_item.confirmed_result is None:
            logging.warning('没有人工确认检测结果。')
            return

        if vi_item.vi_result is None:
            logging.warning('没有AI检测结果。')
            return
        confirm_mode = config.get_config('camera_station.confirm_mode', None)

        if site.get_config('identity.stage', False) == 'cenghou' and confirm_mode != "none":
            if confirm_mode == "ng_only":
                if vi_item.vi_result.is_ng is False:
                    is_confirm_ng = False
                else:
                    flag = vi_item.confirmed_result.ext_info.get("lotGrade", "ng")
                    logging.info("cenghou confirmed_result lotGrade = %s" % flag)
                    if flag == "ok":
                        is_confirm_ng = False
                    else:
                        is_confirm_ng = True
            else:
                flag = vi_item.confirmed_result.ext_info.get("lotGrade", "ng")
                logging.info("cenghou confirmed_result lotGrade = %s" % flag)
                if flag == "ok":
                    is_confirm_ng = False
                else:
                    is_confirm_ng = True
        else:
            is_confirm_ng = True if vi_item.confirmed_result.is_ng else False

        is_ai_ng = True if vi_item.vi_result.is_ng else False
        img_name = os.path.basename(vi_item.el_data.img_info.img_name)
        img_names = os.path.splitext(img_name)
        img_bytes = vi_item.el_data.img_info.img_bytes
        shift = vi_item.el_data.module_info.shift
        shift_date = shift.split('-')[0]
        a_b_dir = img_names[0][-1]  # 取图片的后一位
        if config.get_config('camera_station.el_result_A/B_dir', False):
            shift_date = os.path.join(shift_date, a_b_dir)
        station_type = 'camera_station' if vi_item.el_data.img_info.img_class == 'el' else 'wg_camera_station'
        if is_ai_ng and not is_confirm_ng:
            logging.warning('发现过检图片 %s', vi_item.el_data.img_info.img_name)

            last_overkill_num = status.get_status(
                "ai_accuracy.over_kill", 0)
            status.set_status(
                "ai_accuracy.over_kill", last_overkill_num+1)

            # 保存过检图片
            save_overkill = config.get_config(
                station_type + '.result_save.save_overkill', 0)
            if random.random() < save_overkill and img_bytes:  # 概率取值
                dir_path = os.path.join(save_dir, shift_date, "over_kill")

                if not os.path.exists(dir_path):
                    os.makedirs(dir_path)

                img_path = os.path.join(dir_path, img_name)
                img = img_bytes_2_cv(img_bytes)
                if img_path and img is not None:
                    for vi_rv in vi_item.confirmed_result.defects:
                        txt = '{:.6f}'.format(vi_rv['prob'])
                        font = cv2.FONT_HERSHEY_SIMPLEX
                        minx, miny, maxx, maxy = vi_rv['coord']
                        txt_tl = (minx, miny) if miny > 50 else (minx, maxy+20)
                        cv2.putText(img, txt, txt_tl, font, 1,
                                    (0, 255, 255), lineType=cv2.LINE_AA)
                        cv2.rectangle(img, (minx, miny), (maxx, maxy),
                                      (0, 0, 255), thickness=2)
                    for vi_rv in vi_item.vi_result.defects:
                        txt = '{:.6f}'.format(vi_rv['prob'])
                        font = cv2.FONT_HERSHEY_SIMPLEX
                        minx, miny, maxx, maxy = vi_rv['coord']
                        txt_tl = (minx, miny) if miny > 50 else (minx, maxy+20)
                        cv2.putText(img, txt, txt_tl, font, 1,
                                    (0, 255, 255), lineType=cv2.LINE_AA)
                        cv2.rectangle(img, (minx, miny), (maxx, maxy),
                                      (0, 0, 255), thickness=2)

                    cv2.imwrite(img_path, img)

            # 写入过检信息到csv
            csv_path = os.path.join(save_dir, shift_date, '过检.csv')
            if not os.path.exists(csv_path):
                head = ['序列号', '过检时间', '缺陷名', '行', '列', '置信度']
                df_data = DataFrame([], columns=head)
                df_data.to_csv(csv_path, encoding="utf_8_sig", index=False)

            strtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            img_names = os.path.splitext(img_name)
            for vi_rv in vi_item.vi_result.defects:
                row, col = vi_rv['loc']
                df_data = DataFrame(
                    [[img_names[0], strtime, vi_rv['class'], row, col, vi_rv['prob']]])
                df_data.to_csv(csv_path, mode='a',
                               encoding="utf_8_sig", index=False, header=False)

        if not is_ai_ng and is_confirm_ng:
            logging.warning('发现漏检图片 %s', img_name)

            last_missing = status.get_status(
                "ai_accuracy.missing", 0)
            status.set_status(
                "ai_accuracy.missing", last_missing+1)

            # 保存漏检图片
            save_missing = config.get_config(
                station_type + '.result_save.save_missing', 0)
            if random.random() < save_missing and img_bytes:

                dir_path = os.path.join(save_dir, shift_date, "missing")

                if not os.path.exists(dir_path):
                    os.makedirs(dir_path)
                img_path = os.path.join(dir_path, img_name)
                with open(img_path, 'w+b') as fd:
                    fd.write(img_bytes)

            # 写入csv文件
            csv_path = os.path.join(save_dir, shift_date, '漏检.csv')
            if not os.path.exists(csv_path):
                head = ['序列号', '检测时间']
                df_data = DataFrame([], columns=head)
                df_data.to_csv(csv_path, encoding="utf_8_sig", index=False)
            strtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            df_data = DataFrame([[vi_item.module_id, strtime]])
            df_data.to_csv(csv_path, mode='a',
                           encoding="utf_8_sig", index=False, header=False)

    @staticmethod
    def save_ai_ng_with_box_img(vi_item, dir_path, img_names, img_name, str_time, csv_path):
        details = []
        for vi_rv in vi_item.vi_result.defects:
            row, col = vi_rv['loc']
            details.append(str([vi_rv['class'], row, col, vi_rv['prob']]))
        details = ' | '.join(details)
        df_data = DataFrame([[img_names[0], str_time, 'OK', details]])
        df_data.to_csv(csv_path, mode='a',
                       encoding="utf_8_sig", index=False, header=False)

        def _lable_name2text(name):
            for xx in config.get_config(vi_item.el_data.img_info.img_class + '_defects', []):
                if xx['name'] == name:
                    return xx['text']
            return None

        img_path = os.path.join(dir_path, img_name)
        img_data = img_bytes_2_cv(vi_item.img_bytes)
        for vi_rv in vi_item.vi_result.defects:
            txt = '{:.6f}'.format(vi_rv['prob'])
            font = cv2.FONT_HERSHEY_SIMPLEX
            minx, miny, maxx, maxy = vi_rv['coord']
            offset = 15
            minx = minx - offset
            miny = miny - offset
            maxx = maxx + offset
            maxx = maxx + offset
            txt_tl = (minx, miny) if miny > 50 else (minx, maxy + 20)
            cv2.putText(img_data, txt, txt_tl, font, 1,
                        (0, 255, 255), lineType=cv2.LINE_AA)
            shape = DefectShape(_lable_name2text(vi_rv['class']), [QPointF(0, 0), QPointF(1, 1)])
            line_color = shape.line_color
            cv2.rectangle(img_data, (minx, miny), (maxx, maxy),
                          (line_color.blue(), line_color.green(), line_color.red()), thickness=2)
        cv2.imwrite(img_path, img_data)
        logging.info("With Box Image Write OK")
