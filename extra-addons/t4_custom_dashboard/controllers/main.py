# -*- coding: utf-8 -*-
import inspect
import json
import logging

from odoo import http
from odoo.exceptions import AccessDenied
from odoo.http import request

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain builders cho từng filter type (schema v2)
# ---------------------------------------------------------------------------
def _build_company_domain(field, value):
    if not value:
        return []
    return [(field, 'in', value if isinstance(value, list) else [value])]


def _build_m2m_domain(field, value):
    if not value:
        return []
    return [(field, 'in', value if isinstance(value, list) else [value])]


def _build_m2m_tree_domain(field, value):
    if not value:
        return []
    ids = value if isinstance(value, list) else [value]
    return [(field, 'child_of', ids)]


def _build_date_range_domain(field, value):
    if not value or not isinstance(value, dict):
        return []
    domain = []
    if value.get('from'):
        domain.append((field, '>=', value['from']))
    if value.get('to'):
        domain.append((field, '<=', value['to']))
    return domain


def _build_selection_domain(field, value):
    if value is None or value == '':
        return []
    if isinstance(value, list):
        return [(field, 'in', value)] if value else []
    return [(field, '=', value)]


def _build_char_domain(field, value):
    if not value:
        return []
    return [(field, 'ilike', value)]


DOMAIN_BUILDERS = {
    'company': _build_company_domain,
    'm2m': _build_m2m_domain,
    'm2m_tree': _build_m2m_tree_domain,
    'date_range': _build_date_range_domain,
    'selection': _build_selection_domain,
    'char': _build_char_domain,
}


class CustomDashboardController(http.Controller):
    """Secure Python-backed dashboard runtime."""

    @staticmethod
    def _ensure_dashboard_access():
        user = request.env.user
        if not (
            user.has_group("t4_custom_dashboard.group_dashboard_user")
            or user.has_group("base.group_system")
        ):
            raise AccessDenied()

    @http.route('/t4_custom_dashboard/get_widget_data', type='jsonrpc', auth='user')
    def get_widget_data(self, widget_config):
        self._ensure_dashboard_access()
        try:
            widget_type = widget_config.get('type')
            widget_id = widget_config.get('id')
            data_source = widget_config.get('data_source', {})
            filter_values = widget_config.get('filters', {}) or {}
            search_panel = widget_config.get('search_panel', []) or []

            if not data_source:
                return {'error': 'No data source configured'}

            # Build extra_domain từ filter values + appliesTo + filterOverrides
            extra_domain = self._build_extra_domain(
                widget_id=widget_id,
                filter_values=filter_values,
                search_panel_config=search_panel,
                filter_overrides=data_source.get('filterOverrides') or {},
            )

            source_type = data_source.get('type')
            if source_type == 'python':
                return self._get_python_data(
                    widget_type, data_source, filter_values, extra_domain
                )
            else:
                return {'error': f'Unknown data source type: {source_type}'}
        except Exception as e:
            _logger.error('Error getting widget data: %s', e, exc_info=True)
            return {'error': str(e)}

    def _build_extra_domain(self, widget_id, filter_values, search_panel_config, filter_overrides):
        """Build Odoo domain cho 1 widget cụ thể.

        - Skip filter nếu widget không nằm trong appliesTo (None = apply tất cả).
        - Resolve field: filterOverrides[id] override field mặc định.
        - field=null hoặc không có → không build domain (filter chỉ pass qua filters dict).
        """
        domain = []
        for fdef in (search_panel_config or []):
            if not isinstance(fdef, dict):
                continue
            fid = fdef.get('id')
            if not fid:
                continue
            applies_to = fdef.get('appliesTo')
            if applies_to is not None and widget_id not in applies_to:
                continue
            field_path = filter_overrides.get(fid) or fdef.get('field')
            if not field_path:
                continue
            value = filter_values.get(fid)
            if value in (None, [], ''):
                continue
            ftype = fdef.get('type', 'm2m')
            builder = DOMAIN_BUILDERS.get(ftype)
            if builder:
                try:
                    domain.extend(builder(field_path, value))
                except Exception as exc:
                    _logger.warning('Filter %s build failed: %s', fid, exc)
        return domain

    def _get_python_data(self, widget_type, data_source, filters, extra_domain):
        try:
            model_name = (data_source.get('pythonModel') or '').strip()
            method_name = (data_source.get('pythonMethod') or '').strip()
            params = data_source.get('pythonParams') or {}

            if not model_name or not method_name:
                return {'error': 'Model or method not specified'}
            if not method_name.startswith('get_dashboard_'):
                return {'error': 'Dashboard Python methods must use the get_dashboard_ prefix'}
            if model_name not in request.env:
                return {'error': f'Model {model_name} not found'}

            model = request.env[model_name]
            if not hasattr(model, method_name):
                return {'error': f'Method {method_name} not found in model {model_name}'}

            method = getattr(model, method_name)
            if not callable(method):
                return {'error': f'{method_name} is not callable'}

            # Truyền cả filters (raw) và extra_domain (đã build).
            # Backward compat: nếu method legacy không khai báo `extra_domain`
            # và không có **kwargs, chỉ truyền `filters`.
            call_params = {**params, 'filters': filters}

            # Auto-inject topLimit từ filters → param method (centralized).
            # Convention: ô input "topLimit" trên widget map vào param đầu tiên
            # match theo thứ tự ưu tiên: top > days. Method không có cả 2 thì
            # bỏ qua (vd: stat). Cho phép 1 chỗ UI điều khiển nhiều method
            # mà không cần mỗi method tự đọc filters['topLimit'].
            top_limit_raw = (filters or {}).get('topLimit')
            try:
                top_limit = int(top_limit_raw) if top_limit_raw else None
            except (TypeError, ValueError):
                top_limit = None

            try:
                sig = inspect.signature(method)
                accepts_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                )
                if 'extra_domain' in sig.parameters or accepts_kwargs:
                    call_params['extra_domain'] = extra_domain
                if top_limit and top_limit > 0:
                    if 'top' in sig.parameters:
                        call_params['top'] = top_limit
                    elif 'days' in sig.parameters:
                        call_params['days'] = top_limit
            except (TypeError, ValueError):
                # Builtin / C method — bỏ qua introspection, không truyền extra_domain
                pass
            result = method(**call_params)

            if result is None:
                if widget_type == 'stat':
                    return {'value': 0, 'trend': 'neutral', 'trend_value': 'No data'}
                return {'labels': [], 'values': []}
            return result
        except Exception as e:
            _logger.error('Python function error: %s', e, exc_info=True)
            return {'error': f'Python Error: {str(e)}'}

    # =========================================================================
    # DASHBOARD MANAGEMENT
    # =========================================================================

    @http.route('/t4_custom_dashboard/save_dashboard', type='jsonrpc', auth='user')
    def save_dashboard(self, dashboard_id, name, description, widgets, search_panel=None):
        self._ensure_dashboard_access()
        """Save dashboard. `widgets` là array, `search_panel` (optional) là array filter defs.

        Backend gộp thành schema v2 trước khi serialize.
        """
        try:
            Dashboard = request.env['custom.dashboard']
            payload = self._serialize_v2(widgets or [], search_panel)

            if dashboard_id:
                dashboard = Dashboard.browse(dashboard_id)
                if not dashboard.exists():
                    return {'error': 'Dashboard not found'}
                if dashboard.user_id.id != request.env.user.id:
                    return {'error': 'You can only edit your own dashboards'}
                dashboard.write({
                    'name': name,
                    'description': description,
                    'widget_config': payload,
                })
            else:
                dashboard = Dashboard.create({
                    'name': name,
                    'description': description,
                    'widget_config': payload,
                    'user_id': request.env.user.id,
                    'company_id': request.env.company.id,
                })
            return {
                'success': True,
                'dashboard_id': dashboard.id,
                'name': dashboard.name,
            }
        except Exception as e:
            _logger.error('Error saving dashboard: %s', e, exc_info=True)
            return {'error': str(e)}

    @staticmethod
    def _serialize_v2(widgets, search_panel):
        """Serialize widgets + search_panel thành schema v2 JSON string."""
        return json.dumps({
            'version': 2,
            'searchPanel': search_panel if search_panel else None,
            'widgets': widgets,
        })

    @http.route('/t4_custom_dashboard/get_dashboards', type='jsonrpc', auth='user')
    def get_dashboards(self):
        self._ensure_dashboard_access()
        try:
            return request.env['custom.dashboard'].get_user_dashboards()
        except Exception as e:
            _logger.error('Error getting dashboards: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/get_dashboard', type='jsonrpc', auth='user')
    def get_dashboard(self, dashboard_id):
        self._ensure_dashboard_access()
        try:
            dashboard = request.env['custom.dashboard'].browse(dashboard_id)
            if not dashboard.exists():
                return {'error': 'Dashboard not found'}
            cfg = dashboard.get_widget_config_normalized()
            return {
                'id': dashboard.id,
                'name': dashboard.name,
                'description': dashboard.description,
                'widgets': cfg['widgets'],
                'searchPanel': cfg['searchPanel'],
                'is_default': dashboard.is_default,
                'is_shared': dashboard.is_shared,
                'is_owner': dashboard.user_id.id == request.env.user.id,
            }
        except Exception as e:
            _logger.error('Error getting dashboard: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/get_default_dashboard', type='jsonrpc', auth='user')
    def get_default_dashboard(self, dashboard_xml_id=None):
        self._ensure_dashboard_access()
        try:
            return request.env['custom.dashboard'].get_default_dashboard(
                dashboard_xml_id=dashboard_xml_id,
            )
        except Exception as e:
            _logger.error('Error getting default dashboard: %s', e, exc_info=True)
            return None

    @http.route('/t4_custom_dashboard/delete_dashboard', type='jsonrpc', auth='user')
    def delete_dashboard(self, dashboard_id):
        self._ensure_dashboard_access()
        try:
            dashboard = request.env['custom.dashboard'].browse(dashboard_id)
            if not dashboard.exists():
                return {'error': 'Dashboard not found'}
            if dashboard.user_id.id != request.env.user.id:
                return {'error': 'You can only delete your own dashboards'}
            dashboard.unlink()
            return {'success': True}
        except Exception as e:
            _logger.error('Error deleting dashboard: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/set_default', type='jsonrpc', auth='user')
    def set_default_dashboard(self, dashboard_id):
        self._ensure_dashboard_access()
        try:
            dashboard = request.env['custom.dashboard'].browse(dashboard_id)
            if not dashboard.exists():
                return {'error': 'Dashboard not found'}
            dashboard.action_set_as_default()
            return {'success': True}
        except Exception as e:
            _logger.error('Error setting default: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/duplicate_dashboard', type='jsonrpc', auth='user')
    def duplicate_dashboard(self, dashboard_id):
        self._ensure_dashboard_access()
        try:
            dashboard = request.env['custom.dashboard'].browse(dashboard_id)
            if not dashboard.exists():
                return {'error': 'Dashboard not found'}
            return {'success': True, 'dashboard': dashboard.action_duplicate()}
        except Exception as e:
            _logger.error('Error duplicating dashboard: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/export_dashboard', type='jsonrpc', auth='user')
    def export_dashboard(self, dashboard_id):
        self._ensure_dashboard_access()
        try:
            dashboard = request.env['custom.dashboard'].browse(dashboard_id)
            if not dashboard.exists():
                return {'error': 'Dashboard not found'}
            return dashboard.export_dashboard()
        except Exception as e:
            _logger.error('Error exporting dashboard: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/resolve_view_id', type='jsonrpc', auth='user')
    def resolve_view_id(self, xml_id):
        self._ensure_dashboard_access()
        """Resolve ir.ui.view xml_id → integer id. Cho widget kanban_embed."""
        try:
            view = request.env.ref(xml_id, raise_if_not_found=False)
            if not view or view._name != 'ir.ui.view':
                return {'error': f'View {xml_id} not found'}
            return {'view_id': view.id, 'view_type': view.type}
        except Exception as e:
            _logger.error('Error resolving view_id: %s', e, exc_info=True)
            return {'error': str(e)}

    def _extract_single_id(self, domain):
        """Detect domain dạng [('id', '=', X)] hoặc [('id', 'in', [X])] với 1 phần tử.

        Return id (int) nếu match → drill-down 1 record duy nhất → có thể
        open form trực tiếp thay vì list view. Trả None nếu không match.
        """
        if not isinstance(domain, list) or len(domain) != 1:
            return None
        leaf = domain[0]
        if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
            return None
        field, op, value = leaf
        if field != 'id':
            return None
        if op == '=' and isinstance(value, int):
            return value
        if op == 'in' and isinstance(value, (list, tuple)) and len(value) == 1:
            v0 = value[0]
            if isinstance(v0, int):
                return v0
        return None

    @http.route('/t4_custom_dashboard/resolve_drill_action', type='jsonrpc', auth='user')
    def resolve_drill_action(
        self,
        action_xml_id,
        domain_method=None,
        domain_params=None,
        domain_model=None,
        filters=None,
        search_panel=None,
        widget_id=None,
        filter_overrides=None,
        extra_context=None,
    ):
        self._ensure_dashboard_access()
        """Resolve drill-down using an existing Odoo window action.

        Khác với `resolve_action_domain` (build action từ raw fields):
        endpoint này load action `ir.actions.act_window` đã định nghĩa
        (kèm context HID/RFID scan, views t4_, search view...) rồi AND-merge
        domain drill-down vào `domain` của action. Frontend chỉ cần
        `doAction(action_dict)` — view + context HID giữ nguyên.

        - `action_xml_id`: ID action có sẵn.
        - `domain_method`: method `t4tek.dashboard.report` trả về drill domain.
          Nếu None → mở action với domain mặc định (không drill).
        - `extra_context`: context bổ sung (frontend merge thêm — vd
          `{search_default_X: 1}`).
        """
        try:
            from odoo.tools.safe_eval import safe_eval
            try:
                action_rec = request.env.ref(action_xml_id)
            except ValueError:
                return {'error': f'Action {action_xml_id} not found'}
            if action_rec._name != 'ir.actions.act_window':
                return {'error': f'{action_xml_id} không phải act_window'}

            # Use Odoo's official _for_xml_id loader — returns FULL action dict
            # with views[], context (evaluated dict), domain (evaluated list),
            # search_view_id, help, ... — y hệt khi user click menu chuẩn.
            # KHÔNG dùng .read() vì `views` là computed Binary field bị skip
            # và `context` lại trả về string thay vì dict.
            try:
                action_data = request.env['ir.actions.actions']._for_xml_id(action_xml_id)
            except Exception as e:
                return {'error': f'Cannot load action {action_xml_id}: {e}'}

            # Resolve drill domain
            drill_domain = []
            if domain_method:
                if not domain_method.startswith('get_dashboard_domain_'):
                    return {'error': 'Dashboard domain methods must use the get_dashboard_domain_ prefix'}
                method_model = domain_model or 't4tek.dashboard.report'
                if method_model not in request.env:
                    return {'error': f'Domain model {method_model} not found'}
                DomainModel = request.env[method_model]
                if not hasattr(DomainModel, domain_method):
                    return {
                        'error': f'Method {domain_method} not found in {method_model}'
                    }
                extra_domain = self._build_extra_domain(
                    widget_id=widget_id,
                    filter_values=filters or {},
                    search_panel_config=search_panel or [],
                    filter_overrides=filter_overrides or {},
                )
                method = getattr(DomainModel, domain_method)
                params = dict(domain_params or {})
                params['filters'] = filters or {}
                params['extra_domain'] = extra_domain
                drill_domain = method(**params)
                if not isinstance(drill_domain, list):
                    return {'error': 'domain method must return a list'}

            # Strip help HTML from actions before returning JSON-RPC payload.
            # rồi doAction() → frontend không re-process QWeb template →
            # render literal `<p name="top_help_message">` as text. Bỏ help
            # để view chỉ hiện smiley nocontent state khi list rỗng (không
            # ai cần help từ drill-down).
            action_data['help'] = ''

            # _for_xml_id đã eval domain → list. Nếu vì lý do gì còn là
            # string thì fallback eval (defensive).
            existing_domain = action_data.get('domain') or []
            if isinstance(existing_domain, str):
                try:
                    existing_domain = safe_eval(
                        existing_domain,
                        {
                            'active_id': None,
                            'active_ids': [],
                            'uid': request.env.uid,
                        },
                    )
                except Exception:
                    existing_domain = []

            final_domain = list(existing_domain) + list(drill_domain)
            action_data['domain'] = final_domain

            # 🆕 Single-record drill detection: nếu drill_domain filter id=X
            # (single record) → set res_id + đảo views để form lên đầu.
            # Trải nghiệm tốt hơn: click chart bar (1 SP) → mở form luôn,
            # không qua list view có 1 dòng.
            single_id = self._extract_single_id(drill_domain)
            if single_id is not None:
                action_data['res_id'] = single_id
                views = action_data.get('views') or []
                form_views = [v for v in views if v[1] == 'form']
                other_views = [v for v in views if v[1] != 'form']
                if form_views:
                    action_data['views'] = form_views + other_views
                    modes = [v[1] for v in action_data['views']]
                    action_data['view_mode'] = ','.join(dict.fromkeys(modes))

            # Merge context — `_for_xml_id` đã eval context thành dict
            # (bao gồm RFID buttons, t4_passive_buttons, ...). Chỉ cần
            # spread merge nếu có extra_context từ widget.
            # Placeholder động: giá trị "__clicked_date__"/"__clicked_id__"
            # trong extra_context được thay bằng giá trị click thực tế
            # (recursive qua list/dict). Cho phép widget JSON đẩy ngày/id đã
            # click vào context action đích — vd điền sẵn searchpanel
            # date-range: {"searchpanel_default_report_date":
            # ["__clicked_date__", "__clicked_date__"]}. Không click (mở cả
            # cửa sổ) → placeholder thành False.
            if extra_context:
                subs = {
                    '__clicked_date__': (domain_params or {}).get('clicked_date') or False,
                    '__clicked_id__': (domain_params or {}).get('clicked_id') or False,
                }

                def _subst(val):
                    if isinstance(val, str) and val in subs:
                        return subs[val]
                    if isinstance(val, list):
                        return [_subst(v) for v in val]
                    if isinstance(val, dict):
                        return {k: _subst(v) for k, v in val.items()}
                    return val

                extra_context = _subst(extra_context)
                existing_dict = action_data.get('context') or {}
                if not isinstance(existing_dict, dict):
                    existing_dict = {}
                action_data['context'] = {**existing_dict, **extra_context}

            return {'action': action_data}
        except Exception as e:
            _logger.error('Error resolving drill action: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/resolve_action_domain', type='jsonrpc', auth='user')
    def resolve_action_domain(
        self,
        model,
        domain_method,
        domain_params=None,
        filters=None,
        search_panel=None,
        widget_id=None,
        filter_overrides=None,
        domain_model=None,
    ):
        self._ensure_dashboard_access()
        """Resolve drill-down domain cho widget stat_action.

        Frontend gọi khi user click stat → render list view với domain
        đã merge filter searchPanel (company/location/date) + domain_params
        của widget. Trả về domain list để doAction.

        - `model`: model target sẽ mở list view.
        - `domain_method`: tên method trả về domain.
        - `domain_model`: model chứa `domain_method` — default
          `t4tek.dashboard.report` (data layer). Cho phép override để gọi
          method trên model khác nếu cần.
        """
        try:
            if model not in request.env:
                return {'error': f'Target model {model} not found'}

            if domain_method and not domain_method.startswith('get_dashboard_domain_'):
                return {'error': 'Dashboard domain methods must use the get_dashboard_domain_ prefix'}
            method_model = domain_model or 't4tek.dashboard.report'
            if method_model not in request.env:
                return {'error': f'Domain model {method_model} not found'}
            DomainModel = request.env[method_model]
            if not domain_method or not hasattr(DomainModel, domain_method):
                return {
                    'error': f'Method {domain_method} not found in {method_model}'
                }

            extra_domain = self._build_extra_domain(
                widget_id=widget_id,
                filter_values=filters or {},
                search_panel_config=search_panel or [],
                filter_overrides=filter_overrides or {},
            )
            method = getattr(DomainModel, domain_method)
            params = dict(domain_params or {})
            params['filters'] = filters or {}
            params['extra_domain'] = extra_domain
            domain = method(**params)
            if not isinstance(domain, list):
                return {'error': 'domain method must return a list'}
            return {'domain': domain}
        except Exception as e:
            _logger.error('Error resolving action domain: %s', e, exc_info=True)
            return {'error': str(e)}

    @http.route('/t4_custom_dashboard/import_dashboard', type='jsonrpc', auth='user')
    def import_dashboard(self, config_data, dashboard_name=None):
        self._ensure_dashboard_access()
        try:
            return {
                'success': True,
                'dashboard': request.env['custom.dashboard'].import_dashboard(
                    config_data, dashboard_name
                ),
            }
        except Exception as e:
            _logger.error('Error importing dashboard: %s', e, exc_info=True)
            return {'error': str(e)}
