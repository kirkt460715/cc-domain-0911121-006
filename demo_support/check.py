import json,hashlib,urllib.request
from source import start,ROOT
server,base=start()
try:
    from email import policy
    from email.parser import BytesParser
    listing=json.load(urllib.request.urlopen(base+'/mailbox'));assert listing['mailbox_epoch']=='epoch-1'
    raw=urllib.request.urlopen(base+'/mail/1').read();mail=BytesParser(policy=policy.default).parsebytes(raw)
    assert mail.is_multipart() and len(list(mail.iter_attachments()))==1
    newer=json.load(urllib.request.urlopen(base+'/mailbox?epoch=2'));assert newer['messages'][0]['uid']==1
    assert urllib.request.urlopen(base+'/mail/new1').read()!=raw
finally:
    server.shutdown();server.server_close()
print('本地输入协议与故障样本检查通过')
