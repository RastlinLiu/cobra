# -*- coding: utf-8 -*-

"""
    engine.core
    ~~~~~~~~~~~

    Implements core scan logic

    :author:    Feei <wufeifei#wufeifei.com>
    :homepage:  https://github.com/wufeifei/cobra
    :license:   MIT, see LICENSE for more details.
    :copyright: Copyright (c) 2016 Feei. All rights reserved
"""
import os
import re
import logging
import traceback
from engine import parse
from app import db, CobraResults
from utils.queue import Queue

logging = logging.getLogger(__name__)


class Core:
    def __init__(self, result, rule, project_name, white_list):
        """
        初始化
        :param result: 漏洞信息
                        {'task_id': self.task_id,
                        'project_id': self.project_id,
                        'rule_id': rule.id,
                        'file': file_path,
                        'line_number': line_number,
                        'code_content': code_content}
        :param rule: 规则信息
        :param project_name: 项目名称
        :param white_list: 白名单列表
        """
        self.project_id = result['project_id']
        self.project_directory = result['project_directory']
        self.rule_id = result['rule_id']
        self.task_id = result['task_id']

        self.third_party_vulnerabilities_name = result['third_party_vulnerabilities_name']
        self.third_party_vulnerabilities_type = result['third_party_vulnerabilities_type']

        self.file_path = result['file_path'].strip()
        self.line_number = result['line_number']
        self.code_content = result['code_content'].strip()

        self.rule_location = rule.regex_location.strip()
        self.rule_repair = rule.regex_repair.strip()
        self.block_repair = rule.block_repair

        self.project_name = project_name
        self.white_list = white_list

        self.status = 0

    def is_white_list(self):
        """
        是否是白名单文件
        :return: boolean
        """
        return self.file_path in self.white_list

    def is_special_file(self):
        """
        是否是特殊文件
        :method: 通过判断文件名中是否包含.min.js来判定
        :return: boolean
        """
        return ".min.js" in self.file_path

    def is_match_only_rule(self):
        """
        是否仅仅匹配规则,不做参数可控处理
        :method: 通过判断定位规则(regex_location)的左右两边是否是括号来判定
        :return: boolean
        """
        return self.rule_location[:1] == '(' and self.rule_location[-1] == ')'

    def is_annotation(self):
        """
        是否是注释
        :method: 通过匹配注释符号来判定 (当符合self.is_match_only_rule条件时跳过)
               - PHP:  `#` `//` `\*` `*`
                    //asdfasdf
                    \*asdfasdf
                    #asdfasdf
                    *asdfasdf
               - Java:
        :return: boolean
        """
        match_result = re.findall(r"(#|\\\*|\/\/|\*){1}", self.code_content)
        # 仅仅匹配时跳过检测
        if self.is_match_only_rule():
            return False
        else:
            return len(match_result) > 0

    def push_third_party_vulnerabilities(self, vulnerabilities_id):
        try:
            q = Queue(self.project_name, self.third_party_vulnerabilities_name, self.third_party_vulnerabilities_type, self.file_path, self.line_number, self.code_content, vulnerabilities_id)
            q.push()
        except Exception as e:
            print(traceback.print_exc())
            logging.critical(e.message)

    def process_vulnerabilities(self):
        """
        处理漏洞
        写入漏洞/更改漏洞状态/推送第三方漏洞管理平台
        :return:
        """
        # 处理相对路径
        self.file_path = self.file_path.replace(self.project_directory, '')
        # 行号为0的时候则为搜索特殊文件
        if self.line_number == 0:
            exist_result = CobraResults.query.filter_by(project_id=self.project_id, rule_id=self.rule_id, file=self.file_path).first()
        else:
            exist_result = CobraResults.query.filter_by(project_id=self.project_id, rule_id=self.rule_id, file=self.file_path, line=self.line_number).first()

        # 该漏洞已经被扫出来过
        if exist_result is not None:
            logging.info("Exists Result {0}".format(self.status))
            # 当漏洞状态为初始化时,则推送给第三方漏洞管理平台
            if exist_result.status == 0:
                self.push_third_party_vulnerabilities(exist_result.id)

            # 状态为已修复的话则更新漏洞状态
            if self.status == 2:
                exist_result.status = 2
                db.session.add(exist_result)
                db.session.commit()
                logging.info('Update vulnerabilities status to fixed')

        else:
            vul = CobraResults(self.task_id, self.project_id, self.rule_id, self.file_path, self.line_number, self.code_content, self.status)
            db.session.add(vul)
            db.session.commit()
            self.push_third_party_vulnerabilities(vul.id)

    def analyse(self, method=None):
        """
        通过规则分析漏洞
        :param: method 修复方法 (扫描:None|0 修复: 1)
        :return: (Status, Result)
        """
        # 如果是修复检测
        if method == 1:
            # 拼接绝对路径
            self.file_path = self.project_directory + self.file_path
            # 取出触发代码
            trigger_code = re.findall(r'(?!#\sTrigger)\r(.*)', self.code_content)
            if len(trigger_code) != 1:
                logging.critical("Trigger code match failed {0}".format(self.code_content))
                return False, 4009
            self.code_content = trigger_code[0].strip()
        # 定位规则为空时,表示此类型语言(该语言所有后缀)文件都算作漏洞
        if self.rule_location == '':
            logging.info("Find special files: RID{0}".format(self.rule_id))
            # 修复分析时
            if method == 1:
                # 检查文件是否存在
                if os.path.isfile(self.file_path) is False:
                    # 未找到该文件则更新漏洞状态为已修复
                    logging.info("Already fixed {0}".format(self.file_path))
                    self.status = 2
            self.process_vulnerabilities()
            return True, 1001

        # 白名单
        if self.is_white_list():
            logging.info("In white list {0}".format(self.file_path))
            return False, 4000

        # 特殊文件判断
        if self.is_special_file():
            logging.info("Special File: {0}".format(self.file_path))
            return False, 4001

        # 注释判断
        if self.is_annotation():
            logging.info("In Annotation {0}".format(self.code_content))
            return False, 4002

        param_value = None

        # 仅匹配规则
        if self.is_match_only_rule():
            logging.info("Only match {0}".format(self.rule_location))
            found_vul = True
        else:
            found_vul = False
            # 判断参数是否可控
            if self.file_path[-3:] == 'php' and self.rule_repair.strip() != '':
                try:
                    parse_instance = parse.Parse(self.rule_location, self.file_path, self.line_number, self.code_content)
                    if parse_instance.is_controllable_param():
                        if parse_instance.is_repair(self.rule_repair, self.block_repair):
                            logging.info("Static: repaired")
                            # 标记已修复
                            self.status = 2
                            self.process_vulnerabilities()
                            return True, 1003
                        else:
                            if parse_instance.param_value is not None:
                                param_value = parse_instance.param_value
                            found_vul = True
                    else:
                        logging.info("Static: uncontrollable param")
                        return False, 4004
                except:
                    print(traceback.print_exc())
                    return False, 4005

        if found_vul:
            self.code_content = self.code_content.encode('unicode_escape')
            if len(self.code_content) > 512:
                self.code_content = self.code_content[:500] + '...'
            self.code_content = '# Trigger\r{0}'.format(self.code_content)
            if param_value is not None:
                self.code_content = '# Param\r{0}\r//\r// ------ Continue... ------\r//\r{1}'.format(param_value, self.code_content)
            self.process_vulnerabilities()
            return True, 1002
        else:
            logging.critical("Exception core")
            return False, 4006