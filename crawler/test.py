import requests

r = requests.get("https://www.moltbook.com/api/v1/posts",
                 params={"sort":"new","limit":50,"offset":0},
                 headers={"Authorization": "Bearer YOUR_TOKEN"},
                 allow_redirects=True)

print("final:", r.url)
print("history:", [h.status_code for h in r.history], "->", r.status_code)
print("via:", r.headers.get("Via"), "x-cache:", r.headers.get("X-Cache"))
