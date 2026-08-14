# -*- coding: utf-8 -*-
import os

from odoo import api, models, _
from odoo.exceptions import AccessError


def _is_edge_server(env):
    role = (
        env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
        or os.getenv("NSP_DEPLOYMENT_ROLE")
        or os.getenv("NSP_SERVER_ROLE")
        or "edge_server"
    ).strip().lower()
    return role != "cloud"


def _assert_cloud_master_write(env):
    if (
        _is_edge_server(env)
        and not env.is_superuser()
        and not env.context.get("nsp_master_data_sync")
    ):
        raise AccessError(
            _("Cloud Master Data is read-only on Edge. Change it on Cloud and let NSP Sync refresh the Edge projection.")
        )


class NspUserEdgeProjection(models.Model):
    _inherit = "nsp.user"

    @api.model_create_multi
    def create(self, vals_list):
        _assert_cloud_master_write(self.env)
        return super().create(vals_list)

    def write(self, vals):
        _assert_cloud_master_write(self.env)
        return super().write(vals)

    def unlink(self):
        _assert_cloud_master_write(self.env)
        return super().unlink()


class NspUserFriendshipEdgeProjection(models.Model):
    _inherit = "nsp.user.friendship"

    @api.model_create_multi
    def create(self, vals_list):
        _assert_cloud_master_write(self.env)
        return super().create(vals_list)

    def write(self, vals):
        _assert_cloud_master_write(self.env)
        return super().write(vals)

    def unlink(self):
        _assert_cloud_master_write(self.env)
        return super().unlink()


class NspVehicleEdgeProjection(models.Model):
    _inherit = "nsp.vehicle"

    @api.model_create_multi
    def create(self, vals_list):
        _assert_cloud_master_write(self.env)
        return super().create(vals_list)

    def write(self, vals):
        _assert_cloud_master_write(self.env)
        return super().write(vals)

    def unlink(self):
        _assert_cloud_master_write(self.env)
        return super().unlink()


class NspVehicleBorrowEdgeProjection(models.Model):
    _inherit = "nsp.vehicle.borrow"

    @api.model_create_multi
    def create(self, vals_list):
        _assert_cloud_master_write(self.env)
        return super().create(vals_list)

    def write(self, vals):
        _assert_cloud_master_write(self.env)
        return super().write(vals)

    def unlink(self):
        _assert_cloud_master_write(self.env)
        return super().unlink()
