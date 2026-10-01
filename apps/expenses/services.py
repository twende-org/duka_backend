from django.core.exceptions import ValidationError
from django.db import transaction

# pyrefly: ignore [missing-import]
from apps.expenses.models import Expense
# pyrefly: ignore [missing-import]
from apps.sales.models import Shift
# pyrefly: ignore [missing-import]
from apps.sales.services import apply_daily_summary_delta, is_cash_payment


def _lock_shifts(shift_ids):
    """Lock shift rows in a stable order; an edit may move an expense between two shifts."""
    ids = {shift_id for shift_id in shift_ids if shift_id}
    if not ids:
        return {}
    shifts = Shift.objects.select_for_update().filter(id__in=ids).order_by('id')
    return {shift.id: shift for shift in shifts}


def _apply_expense_effects(expense, sign, shift_map):
    """Apply (sign=1) or reverse (sign=-1) an expense's effect on summary and shift drawer."""
    delta = expense.amount * sign
    apply_daily_summary_delta(
        shop=expense.shop,
        branch=expense.branch,
        date=expense.date,
        total_expenses=delta,
        net_profit=-delta,
    )

    if expense.shift_id and is_cash_payment(expense.payment_method):
        shift = shift_map.get(expense.shift_id)
        if shift is not None:
            if sign == 1 and shift.shop_id != expense.shop_id:
                raise ValidationError("Shift does not belong to this shop.")
            shift.cash_expenses_total += delta
            shift.save(update_fields=['cash_expenses_total', 'updated_at'])


@transaction.atomic
def record_expense(
    shop,
    *,
    recorded_by=None,
    branch=None,
    shift=None,
    category,
    description,
    amount,
    date,
    payment_method='Taslimu',
    reference='',
    paid_to='',
    notes='',
    is_recurring=False,
):
    expense = Expense.objects.create(
        shop=shop,
        branch=branch,
        shift=shift,
        category=category,
        description=description,
        amount=amount,
        date=date,
        payment_method=payment_method,
        reference=reference,
        paid_to=paid_to,
        notes=notes,
        is_recurring=is_recurring,
        recorded_by=recorded_by,
    )
    _apply_expense_effects(expense, sign=1, shift_map=_lock_shifts({expense.shift_id}))
    return expense


@transaction.atomic
def update_expense(expense, **fields):
    locked = Expense.objects.select_for_update().get(id=expense.id)
    new_shift = fields.get('shift', locked.shift)
    shift_map = _lock_shifts({locked.shift_id, new_shift.id if new_shift else None})

    # Reverse the persisted effects before mutating, so the old values are still readable.
    _apply_expense_effects(locked, sign=-1, shift_map=shift_map)

    for name, value in fields.items():
        setattr(locked, name, value)
    locked.save()

    _apply_expense_effects(locked, sign=1, shift_map=shift_map)
    return locked


@transaction.atomic
def delete_expense(expense):
    locked = Expense.objects.select_for_update().get(id=expense.id)
    _apply_expense_effects(locked, sign=-1, shift_map=_lock_shifts({locked.shift_id}))
    locked.delete()
