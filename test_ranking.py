import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from decimal import Decimal
from django.db.models import Sum, Count, Case, When, Value, IntegerField
from tenants.models import Tenant
from sales.models import SalesAgent, SalesSettlement

branch8 = Tenant.objects.get(id=8)
qs = SalesSettlement.objects.filter(tenant=branch8, status='APPROVED')

rows = qs.values('assignment__sales_agent_id').annotate(
    revenue=Sum('sales_amount'),
    units=Sum('sold_quantity'),
    returned=Sum('returned_quantity'),
    penalties=Sum(
        Case(
            When(penalty=True, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    ),
    penalty_amount=Sum('penalty_amount'),
)

stats = {row['assignment__sales_agent_id']: row for row in rows}
print('Raw grouped data:')
for k, v in stats.items():
    name = 'UNASSIGNED' if k is None else f'Agent {k}'
    print(f'  {name}: revenue={v["revenue"]}, units={v["units"]}, penalties={v["penalties"]}, penalty_amt={v["penalty_amount"]}')

data = []
for agent in SalesAgent.objects.filter(tenant=branch8, is_active=True):
    row = stats.get(agent.id, {})
    revenue = (row.get('revenue') or Decimal('0')).quantize(Decimal('0.01'))
    units = row.get('units') or Decimal('0')
    returned = row.get('returned') or Decimal('0')
    penalties = row.get('penalties') or 0
    penalty_amount = (row.get('penalty_amount') or Decimal('0')).quantize(Decimal('0.01'))
    data.append({
        'agent_id': agent.id,
        'name': agent.full_name,
        'revenue': str(revenue),
        'units_sold': str(units),
        'returned_quantity': str(returned),
        'penalty_count': penalties,
        'penalty_amount': str(penalty_amount),
        'has_penalty': penalties > 0,
    })

print()
print('Ranking (active agents only):')
for d in data:
    print(f'  {d["name"]}: revenue={d["revenue"]}, units={d["units_sold"]}, penalties={d["penalty_count"]}, penalty_amt={d["penalty_amount"]}')

data.sort(key=lambda row: (-Decimal(row['revenue']), -Decimal(row['units_sold']), Decimal(row['penalty_amount'])))
print()
print('After sort (revenue desc, units desc, penalty asc):')
for i, d in enumerate(data, 1):
    print(f'  Rank {i}: {d["name"]} - {d["revenue"]}')

# Also check volosync-owner logic for branch 8
print()
print('=== volosync-owner logic for branch 8 ===')
from sales.models import SalesSettlement as OSS
from tenants.models import Tenant as OT
from decimal import Decimal as D

# The owner_views.py logic
agent_rows = qs.filter(assignment__isnull=False).values(
    'assignment__sales_agent_id',
    'assignment__sales_agent__first_name',
    'assignment__sales_agent__last_name',
).annotate(
    revenue=Sum('sales_amount'),
    units=Sum('sold_quantity'),
    settlements=Count('id'),
    penalties=Sum(
        Case(
            When(penalty=True, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    ),
    penalty_amount=Sum('penalty_amount'),
)

agents = []
for row in agent_rows:
    first = row['assignment__sales_agent__first_name'] or ''
    last = row['assignment__sales_agent__last_name'] or ''
    agents.append({
        'id': row['assignment__sales_agent_id'],
        'name': f'{first} {last}'.strip() or 'Unassigned',
        'revenue': row['revenue'] or D('0'),
        'units': row['units'] or D('0'),
        'settlements': row['settlements'],
        'penalties': row['penalties'],
        'penalty_amount': row['penalty_amount'] or D('0'),
    })

print('Agent rows from owner_views logic:')
for a in agents:
    print(f'  {a["name"]}: revenue={a["revenue"]}, units={a["units"]}, penalties={a["penalties"]}, penalty_amt={a["penalty_amount"]}')

# Add quiet agents
quiet = SalesAgent.objects.filter(tenant=branch8, is_active=True).exclude(id__in=[a['id'] for a in agents])
for agent in quiet:
    agents.append({
        'id': agent.id,
        'name': agent.full_name,
        'revenue': D('0'),
        'units': D('0'),
        'settlements': 0,
        'penalties': 0,
        'penalty_amount': D('0'),
    })

agents.sort(key=lambda row: (-row['revenue'], -row['units'], row['penalty_amount']))
for i, a in enumerate(agents, 1):
    a['rank'] = i
    print(f'  Rank {a["rank"]}: {a["name"]} - {a["revenue"]}')