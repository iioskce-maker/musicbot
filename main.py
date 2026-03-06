import requests

# Замените эти переменные вашими собственными данными
client_id = "YOUR_CLIENT_ID"
client_secret = "YOUR_CLIENT_SECRET"

# Получаем токен доступа
auth_url = "https://secure.soundcloud.com/oauth/token"
auth_data = {
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret
}

response = requests.post(auth_url, data=auth_data)
access_token = response.json()["access_token"]

# Теперь ищем треки
search_url = "https://api.soundcloud.com/tracks"
params = {
    "q": "название песни",
    "limit": 10  # количество результатов
}

headers = {
    "Authorization": f"OAuth {access_token}"
}

search_response = requests.get(search_url, params=params, headers=headers)
tracks = search_response.json()

for track in tracks["collection"]:
    print(track["title"])
