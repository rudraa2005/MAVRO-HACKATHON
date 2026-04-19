import urllib.request, json, time

# Force eval cache refresh by waiting 3s
time.sleep(3)

data = json.loads(urllib.request.urlopen('http://127.0.0.1:5000/api/vehicles').read().decode())
print(f"Total vehicles: {len(data)}")
print(f"state=wrong_way: {sum(1 for v in data if v.get('state')=='wrong_way')}")
print(f"wrong_way=True: {sum(1 for v in data if v.get('wrong_way'))}")
print(f"state=suspicious: {sum(1 for v in data if v.get('state')=='suspicious')}")
print(f"state=normal: {sum(1 for v in data if v.get('state')=='normal')}")
print()

# Print suspicious vehicles to see why they're suspicious
for v in data:
    if v.get('state') == 'suspicious':
        print(f"  SUSP V#{v['id']}: conf={v.get('confidence',0):.3f}, angle_diff={v.get('angle_diff',0):.1f}, wwp={v.get('wwp',0):.3f}, road_class={v.get('road_class')}")

# Now inject wrong way
req = urllib.request.Request(
    'http://127.0.0.1:5000/api/admin/scenarios/wrong-way',
    data=json.dumps({'duration_seconds': 45}).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
resp = json.loads(urllib.request.urlopen(req).read().decode())
print(f"\nInjected: vehicle_id={resp['vehicle_id']}")

# Wait for tick + eval cache
time.sleep(3)

data2 = json.loads(urllib.request.urlopen('http://127.0.0.1:5000/api/vehicles').read().decode())
print(f"\nAfter injection:")
print(f"state=wrong_way: {sum(1 for v in data2 if v.get('state')=='wrong_way')}")
print(f"wrong_way=True: {sum(1 for v in data2 if v.get('wrong_way'))}")

analytics = json.loads(urllib.request.urlopen('http://127.0.0.1:5000/api/analytics').read().decode())
print(f"\nEvaluation: {json.dumps(analytics.get('evaluation', {}))}")
