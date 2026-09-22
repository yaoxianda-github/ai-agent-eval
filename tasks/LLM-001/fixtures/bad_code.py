def f(a, b):
    s = 0
    for i in range(len(a)):
        s = s + a[i] * b[i]
    return s

def f2(x):
    if x > 0:
        r = x * x
    else:
        r = 0
    return r

def process_data(data):
    result = []
    for item in data:
        if item['type'] == 'A':
            val = item['value'] * 2
            result.append({'id': item['id'], 'val': val})
        elif item['type'] == 'B':
            val = item['value'] * 3
            result.append({'id': item['id'], 'val': val})
        elif item['type'] == 'C':
            val = item['value'] * 4
            result.append({'id': item['id'], 'val': val})
    return result

def calc(items):
    total = 0
    count = 0
    for i in items:
        total = total + i
        count = count + 1
    if count > 0:
        avg = total / count
    else:
        avg = 0
    return avg
