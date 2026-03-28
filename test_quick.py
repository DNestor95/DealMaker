import sys
sys.path.insert(0, '.')
from dealmaker_generator import generate_events, build_team
from datetime import datetime
import json

team = build_team(salespeople=4, managers=1, bdc_agents=1)
events = generate_events(
    start_date=datetime(2026, 1, 1),
    days=3,
    daily_leads=5,
    team=team,
    dealership_id='DLR-TEST',
    seed=42,
)
types = {}
milestones = {}
for e in events:
    types[e.type] = types.get(e.type, 0) + 1
    if e.type == 'activity.completed':
        ms = e.payload.get('stage_milestone', 'unknown')
        milestones[ms] = milestones.get(ms, 0) + 1

print(f'Total events: {len(events)}')
print(f'Event types: {json.dumps(types, indent=2)}')
print(f'Milestones: {json.dumps(milestones, indent=2)}')
has_score = any('action_score' in e.payload for e in events)
print(f'action_score present: {has_score}')
sample = events[0].to_dict()
print(f'Sample event keys: {list(sample.keys())}')
print(f'Sample payload keys: {list(sample["payload"].keys())}')
print(f'Sample event: {json.dumps(sample, indent=2, default=str)}')
