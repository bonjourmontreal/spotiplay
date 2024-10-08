import requests
import json
import logging
from urllib.parse import urlencode
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from decouple import config
from django.utils.crypto import get_random_string
from django.core.cache import cache
from .models import Leaderboard
from django.contrib.auth.models import User
from django.db.models import Sum
logger = logging.getLogger(__name__)

class SpotifyAuth:
    AUTH_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    CLIENT_CREDENTIALS_URL = "https://accounts.spotify.com/api/token"
    
    @staticmethod
    def get_redirect_uri():
        """Return the appropriate redirect URI based on the DJANGO_SETTINGS_MODULE. (dev or prod)"""

        # Get the DJANGO_SETTINGS_MODULE value
        django_settings_module = config('DJANGO_SETTINGS_MODULE', default='spotiplay.settings.dev')
        
        # Determine the base URL based on the settings module
        if django_settings_module == 'spotiplay.settings.prod':
            base_url = config('PROD_BASE_URL', default='https://spotiplay.onrender.com')
        else:
            base_url = config('DEV_BASE_URL', default='http://localhost:8000')
        
        return base_url + reverse('spotify_callback')

    @staticmethod
    def generate_state():
        """Generate a random state string."""
        state = get_random_string(16)
        cache.set(state, True, timeout=300)  # Store state in cache for 5 minutes
        return state

    @staticmethod
    def get_auth_url():
        """Generate the Spotify authorization URL."""
        state = SpotifyAuth.generate_state()
        params = {
            'response_type': 'code',
            'redirect_uri': SpotifyAuth.get_redirect_uri(),
            'scope': ' '.join(settings.SOCIAL_AUTH_SPOTIFY_SCOPE),
            'client_id': settings.SOCIAL_AUTH_SPOTIFY_KEY,
            'state': state,
            'show_dialog': 'true'  # Force reauthorization
        }
        return f"{SpotifyAuth.AUTH_URL}?{urlencode(params)}"

    @staticmethod
    def exchange_code_for_token(code):
        """Exchange the authorization code for an access token."""
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': SpotifyAuth.get_redirect_uri(),
            'client_id': settings.SOCIAL_AUTH_SPOTIFY_KEY,
            'client_secret': settings.SOCIAL_AUTH_SPOTIFY_SECRET
        }
        response = requests.post(SpotifyAuth.TOKEN_URL, data=data)
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Failed to exchange code: {response.text}")
            return None
        
    @staticmethod
    def get_app_access_token():
        """Get Spotify API access token using Client Credentials Flow (no user authentication)."""
        auth_response = requests.post(
            SpotifyAuth.CLIENT_CREDENTIALS_URL,
            {
                'grant_type': 'client_credentials',
                'client_id': settings.SOCIAL_AUTH_SPOTIFY_KEY,
                'client_secret': settings.SOCIAL_AUTH_SPOTIFY_SECRET,
            }
        )
        if auth_response.status_code == 200:
            return auth_response.json()['access_token']
        else:
            logger.error(f"Failed to get app access token: {auth_response.text}")
            return None

def spotify_auth(request):
    """Redirects the user to Spotify's authorization page."""
    return redirect(SpotifyAuth.get_auth_url())

def handle_spotify_callback(request):
    """Handles the Spotify callback, verifies state, and exchanges code for token."""
    code = request.GET.get('code')
    state = request.GET.get('state')

    # Verify state parameter
    if not code or not state or not cache.get(state):
        return JsonResponse({'error': 'Invalid state parameter or no code provided'}, status=400)
    
    cache.delete(state)  # Remove state from cache

    token_data = SpotifyAuth.exchange_code_for_token(code)
    if token_data and 'access_token' in token_data:
        return token_data
    return None

def spotify_callback(request):
    token_data = handle_spotify_callback(request)
    if token_data:
        access_token = token_data['access_token']
        request.session['access_token'] = access_token
        request.session['refresh_token'] = token_data['refresh_token']
        
        # Fetch Spotify user profile to get the user ID and other details
        user_profile_url = 'https://api.spotify.com/v1/me'
        headers = {'Authorization': f'Bearer {access_token}'}
        user_profile_response = requests.get(user_profile_url, headers=headers)
        
        if user_profile_response.status_code == 200:
            user_profile = user_profile_response.json()
            spotify_user_id = user_profile['id']
            display_name = user_profile.get('display_name', '')

            # Log the user profile details
            # print(f"Spotify User ID: {spotify_user_id}")
            # print(f"Display Name: {display_name}")

            # Create or get the user based on Spotify data
            user, created = User.objects.get_or_create(username=spotify_user_id)
            if created:
                user.first_name = display_name
                user.set_unusable_password()  # No password needed for Spotify login
                user.save()
            
            # Log user creation status
            # print(f"User created: {created}")
            # print(f"User ID: {user.id}")
            
            request.session['user_id'] = user.id  # Save the user ID in the session
            request.session['spotify_user_id'] = spotify_user_id  # Save the Spotify user ID in the session
            request.session['display_name'] = display_name  # Save the display name in the session
            return redirect('welcome')  # Redirect to the quiz view after successful authentication
        else:
            print("Failed to fetch user profile from Spotify")
            return JsonResponse({'error': 'Failed to fetch user profile from Spotify'}, status=400)
    
    print("Failed to exchange token or Spotify denied the request")
    return JsonResponse({'error': 'Failed to exchange token or Spotify denied the request'}, status=400)

class SpotifyAPI:
    BASE_URL = 'https://api.spotify.com/v1'

    @staticmethod
    def fetch_user_data(access_token, limit=50, time_range='medium_term'):
        """Fetch user-specific data after successful authentication."""
        return {'top_tracks': SpotifyAPI.fetch_user_top_tracks(access_token, limit=limit, time_range=time_range)}

    @staticmethod
    def fetch_user_top_tracks(access_token, limit=50, time_range='medium_term'):
        """Fetches the user's top tracks from Spotify based on the time range"""
        headers = {'Authorization': f'Bearer {access_token}'}
        url = f'{SpotifyAPI.BASE_URL}/me/top/tracks?limit={limit}&time_range={time_range}'
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch top tracks: {response.status_code}")
            return {'error': 'Failed to fetch top tracks', 'status_code': response.status_code}

        top_tracks = response.json().get('items', [])
        simplified_tracks = [
            {
                'name': track['name'],
                'artist': ', '.join(artist['name'] for artist in track['artists']),
                'preview_url': track['preview_url']
            }
            for track in top_tracks
        ]
        return simplified_tracks
    
    @staticmethod
    def fetch_playlist_tracks(access_token, playlist_id, limit=50):
        """Fetch tracks from a specified playlist."""
        headers = {'Authorization': f'Bearer {access_token}'}
        url = f'{SpotifyAPI.BASE_URL}/playlists/{playlist_id}/tracks?limit={limit}'
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            logger.error(f"Failed to fetch playlist tracks: {response.status_code}")
            return {'error': 'Failed to fetch playlist tracks', 'status_code': response.status_code}

        playlist_tracks = response.json().get('items', [])
        simplified_tracks = [
            {
                'name': track['track']['name'],
                'artist': ', '.join(artist['name'] for artist in track['track']['artists']),
                'preview_url': track['track']['preview_url']
            }
            for track in playlist_tracks if track['track']['preview_url'] is not None  # Ensure we only include tracks with preview URLs
        ]

        return simplified_tracks

def logout(request):
    """Log the user out by clearing Spotify authentication data."""
    # Clear Spotify authentication data from the session
    keys_to_clear = ['access_token', 'refresh_token', 'user_id', 'display_name']
    for key in keys_to_clear:
        if key in request.session:
            del request.session[key]

    request.session.flush()  # Clear all session data

    return redirect('index')

def index(request):
    """Render the landing page."""
    return render(request, 'quiz/index.html')

def profile(request):
    """Render the profile page."""
    # Check if the user is authenticated via Spotify or using the 'Guest' profile
    if 'access_token' not in request.session:
        # Non-authenticated user; use 'Guest' profile data
        user = User.objects.get(username='Guest')
        display_name = user.first_name
        user_id = user.id
    else:
        # Authenticated user
        user_id = request.session.get('user_id')
        display_name = request.session.get('display_name')

    if not user_id:
        print("User ID not found in session")
        return redirect('index')

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        print("User not found in database")
        return redirect('index')

    # Fetch highest score, total score, and times played
    highest_score = Leaderboard.objects.filter(user=user).order_by('-score').first()
    total_score = Leaderboard.objects.filter(user=user).aggregate(Sum('score'))['score__sum'] or 0
    times_played = Leaderboard.objects.filter(user=user).count()

    context = {
        'display_name': display_name,
        'spotify_user_id': request.session.get('spotify_user_id', None),
        'highest_score': highest_score.score if highest_score else 0,
        'total_score': total_score,
        'times_played': times_played,
    }

    return render(request, 'quiz/profile.html', context)

def welcome(request):
    """Render the welcome page."""
    if 'access_token' not in request.session:
        # Non-authenticated user, use or create the 'Guest' user
        user, created = User.objects.get_or_create(username='Guest', defaults={'first_name': 'Guest'})
        request.session['user_id'] = user.id
        request.session['display_name'] = user.first_name

    display_name = request.session.get('display_name')
    return render(request, 'quiz/welcome.html', {'display_name': display_name})

def leaderboard(request):
    """Render the leaderboard page."""
    # Both authenticated and 'Guest' users should have access
    return render(request, 'quiz/leaderboard.html')

def quiz(request):
    """Render the quiz page."""
    # No need to create the 'Guest' user here anymore
    return render(request, 'quiz/quiz.html')

def results(request):
    """Render the results page."""
    # Ensure that both authenticated and 'Guest' users can access the results
    score = request.session.get('score', 0)
    return render(request, 'quiz/results.html', {'score': score})

def quiz_data(request):
    """Handle quiz data request: fetch user's top tracks from Spotify API or a hardcoded playlist."""
    time_range = request.GET.get('time_range', 'medium_term')  # Default to medium_term
    
    # If user is logged in and has an access token, use their data
    access_token = request.session.get('access_token')
    if access_token:
        top_tracks = SpotifyAPI.fetch_user_top_tracks(access_token, time_range=time_range)
        if 'error' in top_tracks:
            return JsonResponse(top_tracks, status=top_tracks.get('status_code', 500))
        return JsonResponse({'top_tracks': top_tracks})

    # If user is not logged in, fetch playlist using app credentials
    app_access_token = SpotifyAuth.get_app_access_token()
    if app_access_token:
        top_50_playlist_id = '37i9dQZEVXbMDoHDwVN2tF'
        predefined_tracks = SpotifyAPI.fetch_playlist_tracks(app_access_token, top_50_playlist_id)
        return JsonResponse({'top_tracks': predefined_tracks})
    
    return JsonResponse({'error': 'Unable to fetch playlist'}, status=500)

def submit_score(request):
    """Handle POST request to submit a user's score."""
    if request.method == 'POST':
        try:
            # Retrieve score from request body
            score = json.loads(request.body).get('score')
            if score is None:
                logger.error("Score not found in request body")
                return JsonResponse({'error': 'Score not provided'}, status=400)

            # Check if user is authenticated or non-authenticated
            user_id = request.session.get('user_id')
            if not user_id:
                # Non-authenticated user; use or create 'Guest' user
                user, created = User.objects.get_or_create(username='Guest', defaults={'first_name': 'Guest'})
            else:
                # Authenticated user
                user = User.objects.get(id=user_id)

            # Create leaderboard entry for the user
            Leaderboard.objects.create(user=user, score=score)
            request.session['score'] = score
            logger.info(f"Score submitted successfully for user {user.username}")
            return JsonResponse({'status': 'success'})
        except Exception as e:
            logger.error(f"Error in submit_score: {e}")
            return JsonResponse({'error': 'Internal server error'}, status=500)
    
    # Handle invalid request method
    logger.error("Invalid request method")
    return JsonResponse({'error': 'Invalid request'}, status=400)


def get_leaderboard(request):
    """Retrieve the top 20 scores from the leaderboard."""
    top_scores = Leaderboard.objects.all().order_by('-score')[:20]
    data = [
        {
            'username': entry.user.username,
            'display_name': entry.user.first_name,
            'score': entry.score
        }
        for entry in top_scores
    ]

    return JsonResponse({'leaderboard': data})

def get_total_leaderboard(request):
    """Retrieve the total scores of the top 20 users."""
    # Query the total scores of users and order by total score, limited to top 20
    total_scores = Leaderboard.objects.values('user').annotate(total_score=Sum('score')).order_by('-total_score')[:20]
    data = [
        {
            'username': User.objects.get(id=entry['user']).username,
            'display_name': User.objects.get(id=entry['user']).first_name,
            'total_score': entry['total_score']
        }
        for entry in total_scores
    ]

    return JsonResponse({'total_leaderboard': data})
