# -*- coding: utf-8 -*-

import json
import os

from common_helper_files import get_binary_from_file
from flask import render_template, request, render_template_string

from helperFunctions.dataConversion import none_to_none
from helperFunctions.fileSystem import get_src_dir
from helperFunctions.mongo_task_conversion import check_for_errors, convert_analysis_task_to_fw_obj, create_re_analyze_task
from helperFunctions.web_interface import ConnectTo, get_template_as_string
from helperFunctions.web_interface import overwrite_default_plugins
from intercom.front_end_binding import InterComFrontEndBinding
from objects.firmware import Firmware
from security_switch import roles_accepted, PRIVILEGES
from storage.db_interface_admin import AdminDbInterface
from storage.db_interface_frontend import FrontEndDbInterface
from storage.db_interface_view_sync import ViewReader
from web_interface.components.component_base import ComponentBase
from web_interface.components.compare_routes import get_comparison_uid_list_from_session


def get_analysis_view(view_name):
    view_path = os.path.join(get_src_dir(), 'web_interface/templates/analysis_plugins/{}.html'.format(view_name))
    return get_binary_from_file(view_path).decode('utf-8')


class AnalysisRoutes(ComponentBase):

    analysis_generic_view = get_analysis_view('generic')
    analysis_unpacker_view = get_analysis_view('unpacker')

    def _init_component(self):
        self._app.add_url_rule('/update-analysis/<uid>', 'update-analysis/<uid>', self._update_analysis, methods=['GET', 'POST'])
        self._app.add_url_rule('/analysis/<uid>', 'analysis/<uid>', self._show_analysis_results)
        self._app.add_url_rule('/analysis/<uid>/ro/<root_uid>', '/analysis/<uid>/ro/<root_uid>', self._show_analysis_results)
        self._app.add_url_rule('/analysis/<uid>/<selected_analysis>', '/analysis/<uid>/<selected_analysis>', self._show_analysis_results)
        self._app.add_url_rule('/analysis/<uid>/<selected_analysis>/ro/<root_uid>', '/analysis/<uid>/<selected_analysis>/<root_uid>', self._show_analysis_results)
        self._app.add_url_rule('/admin/re-do_analysis/<uid>', '/admin/re-do_analysis/<uid>', self._re_do_analysis, methods=['GET', 'POST'])

    @staticmethod
    def _get_firmware_ids_including_this_file(fo):
        if isinstance(fo, Firmware):
            return None
        else:
            return list(fo.get_virtual_file_paths().keys())

    @roles_accepted(*PRIVILEGES['view_analysis'])
    def _show_analysis_results(self, uid, selected_analysis=None, root_uid=None):
        root_uid = none_to_none(root_uid)
        other_versions = None

        uids_for_comparison = get_comparison_uid_list_from_session()

        analysis_filter = [selected_analysis] if selected_analysis else []
        with ConnectTo(FrontEndDbInterface, self._config) as sc:
            file_obj = sc.get_object(uid, analysis_filter=analysis_filter)
        if isinstance(file_obj, Firmware):
            root_uid = file_obj.get_uid()
            other_versions = sc.get_other_versions_of_firmware(file_obj)
        if file_obj:
            view = self._get_analysis_view(selected_analysis) if selected_analysis else get_template_as_string('show_analysis.html')
            with ConnectTo(FrontEndDbInterface, self._config) as sc:
                summary_of_included_files = sc.get_summary(file_obj, selected_analysis) if selected_analysis else None
                analysis_of_included_files_complete = not sc.all_uids_found_in_database(list(file_obj.files_included))
            firmware_including_this_fo = self._get_firmware_ids_including_this_file(file_obj)
            with ConnectTo(InterComFrontEndBinding, self._config) as sc:
                analysis_plugins = sc.get_available_analysis_plugins()
            return render_template_string(view,
                                          uid=uid,
                                          firmware=file_obj,
                                          selected_analysis=selected_analysis,
                                          all_analyzed_flag=analysis_of_included_files_complete,
                                          summary_of_included_files=summary_of_included_files,
                                          root_uid=root_uid,
                                          firmware_including_this_fo=firmware_including_this_fo,
                                          analysis_plugin_dict=analysis_plugins,
                                          other_versions=other_versions,
                                          uids_for_comparison=uids_for_comparison)
        else:
            return render_template('uid_not_found.html', uid=uid)

    def _get_analysis_view(self, selected_analysis):
        if selected_analysis == 'unpacker':
            return self.analysis_unpacker_view
        else:
            with ConnectTo(ViewReader, self._config) as vr:
                view = vr.get_view(selected_analysis)
            if view:
                return view.decode('utf-8')
            else:
                return self.analysis_generic_view

    @roles_accepted(*PRIVILEGES['submit_analysis'])
    def _update_analysis(self, uid, re_do=False):
        error = {}
        if request.method == 'POST':
            analysis_task = create_re_analyze_task(request, uid=uid)
            error = check_for_errors(analysis_task)
            if not error:
                self._schedule_re_analysis_task(uid, analysis_task, re_do)
                return render_template('upload/upload_successful.html', uid=uid)

        with ConnectTo(FrontEndDbInterface, self._config) as sc:
            old_firmware = sc.get_firmware(uid=uid, analysis_filter=[])
        if old_firmware is None:
            return render_template('uid_not_found.html', uid=uid)

        with ConnectTo(FrontEndDbInterface, self._config) as sc:
            device_class_list = sc.get_device_class_list()
        device_class_list.remove(old_firmware.device_class)

        with ConnectTo(FrontEndDbInterface, self._config) as sc:
            vendor_list = sc.get_vendor_list()
        vendor_list.remove(old_firmware.vendor)

        with ConnectTo(FrontEndDbInterface, self._config) as sc:
            device_name_dict = sc.get_device_name_dict()
        device_name_dict[old_firmware.device_class][old_firmware.vendor].remove(old_firmware.device_name)

        previously_processed_plugins = list(old_firmware.processed_analysis.keys())
        with ConnectTo(InterComFrontEndBinding, self._config) as sc:
            plugin_dict = overwrite_default_plugins(sc, previously_processed_plugins)

        if re_do:
            title = 're-do analysis'
        else:
            title = 'update analysis'

        return render_template(
            'upload/re-analyze.html',
            device_classes=device_class_list,
            vendors=vendor_list,
            error=error,
            device_names=json.dumps(device_name_dict, sort_keys=True),
            firmware=old_firmware,
            analysis_plugin_dict=plugin_dict,
            title=title
        )

    def _schedule_re_analysis_task(self, uid, analysis_task, re_do):
        fw = convert_analysis_task_to_fw_obj(analysis_task)
        if re_do:
            with ConnectTo(AdminDbInterface, self._config) as sc:
                sc.delete_firmware(uid, delete_root_file=False)
        with ConnectTo(InterComFrontEndBinding, self._config) as sc:
            sc.add_re_analyze_task(fw)

    @roles_accepted(*PRIVILEGES['delete'])
    def _re_do_analysis(self, uid):
        return self._update_analysis(uid, re_do=True)
