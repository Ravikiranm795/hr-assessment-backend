"""M6 Open roles (spec 6/M6). Cross-module interfaces:

- ``service.RoleService.get_open_linked_role(session, org_id, role_id) -> (OpenRole, version_id)``
  (M7 invites; raises ``ROLE_NOT_OPEN`` / ``ROLE_HAS_NO_ASSESSMENT``)
- ``hooks.on_role_close`` registry (M7 registers ``revoke_unstarted``)
"""
