from flask import Flask, request

app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is Running!"

@app.route('/callback')
def spotify_callback():
    # Spotify sends the code or error as query parameters
    code = request.args.get('code')
    error = request.args.get('error')
    if error:
        return f"Error during Spotify authentication: {error}"
    # If everything went well, you'll get a code.
    # The SpotifyOAuth manager in spotipy will handle the code automatically.
    return "Spotify authentication successful. You can close this page."
