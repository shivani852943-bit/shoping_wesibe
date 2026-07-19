import requests, base64, os

BASE = 'http://127.0.0.1:5000'
S = requests.Session()

# Create an admin account (signup)
resp = S.post(BASE + '/admin/signup', data={
    'name': 'AutoAdmin',
    'email': 'autoadmin@example.com',
    'password': 'password123'
}, allow_redirects=True)
print('signup:', resp.status_code)

# Write a tiny PNG to static/images/
img_b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII='
img_path = os.path.join('static', 'images', 'test_upload.png')
with open(img_path, 'wb') as f:
    f.write(base64.b64decode(img_b64))
print('written:', img_path, os.path.exists(img_path))

# Upload product with image_file
files = {'image_file': open(img_path, 'rb')}
data = {'name': 'Auto Product', 'price': '123', 'stock': '5', 'category': 'test', 'description': 'Uploaded by test'}
resp2 = S.post(BASE + '/admin/product/add', data=data, files=files, allow_redirects=True)
print('add product:', resp2.status_code)

# Fetch admin panel to check product presence
resp3 = S.get(BASE + '/admin/panel')
print('panel:', resp3.status_code)
print(resp3.text[:600])
