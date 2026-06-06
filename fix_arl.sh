#!/bin/bash
python3 -c "
import re
with open('/root/.config/streamrip/config.toml', 'r') as f:
    content = f.read()
content = re.sub(r'arl = \".*?\"', 'arl = \"$(grep DEEZER_ARL /app/.env | cut -d= -f2)\"', content)
with open('/root/.config/streamrip/config.toml', 'w') as f:
    f.write(content)
print('ARL injected')
"
