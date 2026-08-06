_ASSIGNMENT_DISPLAY_FIELDS = (
    "active_rfid_assignment_id",
    "rfid_tag_id",
    "rfid_tid",
    "rfid_tid_input",
)


def reload_action():
    return {"type": "ir.actions.client", "tag": "reload"}


def compute_active_assignment(records, target_field):
    Assignment = records.env["nsp.rfid.tag.assignment"].sudo()
    record_ids = [record.id for record in records if isinstance(record.id, int)]
    assignments = Assignment.search(
        [(target_field, "in", record_ids), ("state", "=", "active")],
        order="assigned_at desc, id desc",
    ) if record_ids else Assignment.browse()

    assignment_by_target = {}
    for assignment in assignments:
        target = assignment[target_field]
        if target:
            assignment_by_target.setdefault(target.id, assignment)

    empty = Assignment.browse()
    for record in records:
        assignment = assignment_by_target.get(record.id, empty)
        record.active_rfid_assignment_id = assignment
        record.rfid_tag_id = assignment.tag_id if assignment else False
        record.rfid_tid = assignment.tid if assignment else False


def compute_tid_input(records):
    for record in records:
        record.rfid_tid_input = record.rfid_tid or False


def inverse_tid_input(records):
    Assignment = records.env["nsp.rfid.tag.assignment"]
    for record in records:
        if record.rfid_tid_input:
            Assignment.assign_tid(record, record.rfid_tid_input)


def invalidate_assignment_display(records):
    existing = records.exists()
    if existing:
        existing.invalidate_recordset(list(_ASSIGNMENT_DISPLAY_FIELDS))


def revoke_target_assignments(records, target_field):
    record_ids = [record.id for record in records if isinstance(record.id, int)]
    assignments = records.env["nsp.rfid.tag.assignment"].sudo().search(
        [(target_field, "in", record_ids), ("state", "=", "active")]
    ) if record_ids else records.env["nsp.rfid.tag.assignment"].browse()
    if assignments:
        assignments.with_context(
            rfid_audit_user_id=records.env.user.id
        ).action_revoke()
    invalidate_assignment_display(records)
    return reload_action()


def revoke_assignments_before_archive(records, values, target_field):
    record_ids = [record.id for record in records if isinstance(record.id, int)]
    if values.get("active") is not False or not record_ids:
        return

    assignments = records.env["nsp.rfid.tag.assignment"].sudo().search(
        [(target_field, "in", record_ids), ("state", "=", "active")]
    )
    if assignments:
        assignments.with_context(
            rfid_audit_user_id=records.env.user.id
        ).action_revoke()
