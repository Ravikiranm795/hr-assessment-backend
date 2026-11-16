"""M7 Invites, help requests, accommodations (spec 6/M7, 8.6). Cross-module interfaces:

- ``service.InviteService.create`` / ``revoke_unstarted`` / ``regenerate_link`` / ``reset_binding``
- ``service.HelpRequestService.approve`` / ``decline``
- ``revoke_unstarted`` is registered on ``m06_roles.hooks.on_role_close`` when the router loads.
"""
