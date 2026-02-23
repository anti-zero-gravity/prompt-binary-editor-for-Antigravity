import os
filepath = r'c:\Users\YK-PC\.gemini\antigravity\scratch\syspro\system_prompts_jpn.md'
with open(filepath, 'rb') as f:
    text = f.read()

target1 = b'If you find minor issues or bugs during testing'
target2 = b'VERIFICATION: Test your changes'

indices1 = []
start = 0
while True:
    start = text.find(target1, start)
    if start == -1: break
    indices1.append(start)
    start += len(target1)

indices2 = []
start = 0
while True:
    start = text.find(target2, start)
    if start == -1: break
    indices2.append(start)
    start += len(target2)

print(f"Occurrences of 'If you find minor issues...': {len(indices1)} at positions {indices1}")
print(f"Occurrences of 'VERIFICATION: Test your changes...': {len(indices2)} at positions {indices2}")

for i, pos in enumerate(indices1):
    print(f"\n--- Instance {i+1} at {pos} ---")
    print(text[max(0, pos-50):min(len(text), pos+300)])
