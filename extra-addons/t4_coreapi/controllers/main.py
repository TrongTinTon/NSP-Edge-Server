# Part of T4 Core API. See LICENSE file for full copyright and licensing details.
# Gateway routes are handled by controllers/proxy.py via core.api.endpoint records.
# Import CoreApiController from controllers.base for custom extensions.

from .base import CoreApiController

__all__ = ['CoreApiController']
