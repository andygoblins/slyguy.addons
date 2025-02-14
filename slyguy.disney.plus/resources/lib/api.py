import json
import uuid
from time import time

from slyguy import userdata, settings, mem_cache
from slyguy.session import Session
from slyguy.exceptions import Error
from slyguy.util import get_kodi_setting, jwt_data
from slyguy.log import log

from kodi_six import xbmc

from . import queries
from .constants import *
from .language import _

class APIError(Error):
    pass

ERROR_MAP = {
    'not-entitled': _.NOT_ENTITLED,
    'idp.error.identity.bad-credentials': _.BAD_CREDENTIALS,
    'account.profile.pin.invalid': _.BAD_PIN,
}

class API(object):
    def new_session(self):
        self.logged_in = False
        self._session = Session(HEADERS, timeout=30)
        self._set_authentication(userdata.get('access_token'))

    @mem_cache.cached(60*60, key='config')
    def get_config(self):
        return self._session.get(CONFIG_URL).json()

    @mem_cache.cached(60*60, key='transaction_id')
    def _transaction_id(self):
        return str(uuid.uuid4())

    @mem_cache.cached(60*60, key='profile')
    def _get_profile(self):
        data = self.account()

        profile = None
        if data['account']['activeProfile']:
            for row in data['account']['profiles']:
                if row['id'] == data['account']['activeProfile']['id']:
                    profile = row
                    break

        return data['activeSession'], profile

    @property
    def session(self):
        return self._session

    def _set_authentication(self, access_token):
        if not access_token:
            return

        self._session.headers.update({'Authorization': 'Bearer {}'.format(access_token)})
        self._session.headers.update({'x-bamsdk-transaction-id': self._transaction_id()})
        self.logged_in = True

    def _refresh_token(self, force=False):
        if not force and userdata.get('expires', 0) > time():
            return

        payload = {
            'grant_type': 'refresh_token',
            'refresh_token': userdata.get('refresh_token'),
            'platform': 'android-tv',
        }

        endpoint = self.get_config()['services']['token']['client']['endpoints']['exchange']['href']
        data = self._session.post(endpoint, data=payload, headers={'authorization': 'Bearer {}'.format(API_KEY)}).json()
        self._check_errors(data)
        self._set_auth(data)

    def _set_auth(self, data):
        token = data.get('accessToken') or data['access_token']
        expires = data.get('expiresIn') or data['expires_in']
        refresh_token = data.get('refreshToken') or data['refresh_token']

        self._set_authentication(token)
        userdata.set('access_token', token)
        userdata.set('expires', int(time() + expires - 15))
        userdata.set('refresh_token', refresh_token)

    def login(self, username, password):
        self.logout()

        payload = {
            'variables': {
                'registerDevice': {
                    'applicationRuntime': 'android',
                    'attributes': {
                        'operatingSystem': 'Android',
                        'operatingSystemVersion': '8.1.0',
                    },
                    'deviceFamily': 'android',
                    'deviceLanguage': 'en',
                    'deviceProfile': 'tv',
                }
            },
            'query': queries.REGISTER_DEVICE,
        }

        endpoint = self.get_config()['services']['orchestration']['client']['endpoints']['registerDevice']['href']
        data = self._session.post(endpoint, json=payload, headers={'authorization': API_KEY}).json()
        self._check_errors(data)
        token = data['extensions']['sdk']['token']['accessToken']

        payload = {
            'operationName': 'loginTv',
            'variables': {
                'input': {
                    'email': username,
                    'password': password,
                },
            },
            'query': queries.LOGIN,
        }

        endpoint = self.get_config()['services']['orchestration']['client']['endpoints']['query']['href']
        data = self._session.post(endpoint, json=payload, headers={'authorization': token}).json()
        self._check_errors(data)
        self._set_auth(data['extensions']['sdk']['token'])

    def _check_errors(self, data, error=_.API_ERROR):
        if not type(data) is dict:
            return

        if data.get('errors'):
            if 'extensions' in data['errors'][0]:
                code = data['errors'][0]['extensions'].get('code')
            else:
                code = data['errors'][0].get('code')

            error_msg = ERROR_MAP.get(code) or data['errors'][0].get('message') or data['errors'][0].get('description') or code
            raise APIError(_(error, msg=error_msg))

        elif data.get('error'):
            error_msg = ERROR_MAP.get(data.get('error_code')) or data.get('error_description') or data.get('error_code')
            raise APIError(_(error, msg=error_msg))

        elif data.get('status') == 400:
            raise APIError(_(error, msg=data.get('message')))

    def _json_call(self, endpoint):
        self._refresh_token()
        data = self._session.get(endpoint).json()
        self._check_errors(data)
        return data

    def account(self):
        self._refresh_token()

        endpoint = self.get_config()['services']['orchestration']['client']['endpoints']['query']['href']

        payload = {
            'operationName': 'EntitledGraphMeQuery',
            'variables': {},
            'query': queries.ENTITLEMENTS,
        }

        data = self._session.post(endpoint, json=payload).json()
        self._check_errors(data)
        return data['data']['me']

    def switch_profile(self, profile_id, pin=None):
        self._refresh_token()

        payload = {
            'operationName': 'switchProfile',
            'variables': {
                'input': {
                    'profileId': profile_id,
                },
            },
            'query': queries.SWITCH_PROFILE,
        }

        if pin:
            payload['variables']['input']['entryPin'] = str(pin)

        endpoint = self.get_config()['services']['orchestration']['client']['endpoints']['query']['href']
        data = self._session.post(endpoint, json=payload).json()
        self._check_errors(data)
        self._set_auth(data['extensions']['sdk']['token'])
        mem_cache.delete('profile')

    def _endpoint(self, href, **kwargs):
        session, profile = self._get_profile()

        region = session['location']['countryCode']
        maturity = session['preferredMaturityRating']['impliedMaturityRating'] if session['preferredMaturityRating'] else 1850
        kids_mode = profile['attributes']['kidsModeEnabled'] if profile else False
        appLanguage = profile['attributes']['languagePreferences']['appLanguage'] if profile else 'en-US'

        _args = {
            'apiVersion': API_VERSION,
            'region': region,
            'impliedMaturityRating': maturity,
            'kidsModeEnabled': 'true' if kids_mode else 'false',
            'appLanguage': appLanguage,
        }
        _args.update(**kwargs)

        return href.format(**_args)

    def search(self, query):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getSiteSearch']['href'], query=query)
        return self._json_call(endpoint)['data']['disneysearch']

    def avatar_by_id(self, ids):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getAvatars']['href'], avatarIds=','.join(ids))
        return self._json_call(endpoint)['data']['Avatars']

    def video_bundle(self, family_id):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getDmcVideoBundle']['href'], encodedFamilyId=family_id)
        return self._json_call(endpoint)['data']['DmcVideoBundle']

    def up_next(self, content_id):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getUpNext']['href'], contentId=content_id)
        return self._json_call(endpoint)['data']['UpNext']

    def continue_watching(self):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getCWSet']['href'], setId=CONTINUE_WATCHING_SET_ID)
        return self._json_call(endpoint)['data']['ContinueWatchingSet']

    def add_watchlist(self, content_id):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['addToWatchlist']['href'], contentId=content_id)
        return self._json_call(endpoint)['data']['AddToWatchlist']

    def delete_watchlist(self, content_id):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['deleteFromWatchlist']['href'], contentId=content_id)
        return self._json_call(endpoint)['data']['DeleteFromWatchlist']

    def collection_by_slug(self, slug, content_class):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getCompleteStandardCollection']['href'], contentClass=content_class, slug=slug)
        return self._json_call(endpoint)['data']['CompleteStandardCollection']

    def set_by_id(self, set_id, set_type, page=1, page_size=15):
        if set_type == 'ContinueWatchingSet':
            endpoint = 'getCWSet'
        elif set_type == 'CuratedSet':
            endpoint = 'getCuratedSet'
        else:
            endpoint = 'getSet'

        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints'][endpoint]['href'], setType=set_type, setId=set_id, pageSize=page_size, page=page)
        return self._json_call(endpoint)['data'][set_type]

    def video(self, content_id):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getDmcVideo']['href'], contentId=content_id)
        return self._json_call(endpoint)['data']['DmcVideo']

    def series_bundle(self, series_id):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getDmcSeriesBundle']['href'], encodedSeriesId=series_id)
        return self._json_call(endpoint)['data']['DmcSeriesBundle']

    def episodes(self, season_id, page=1, page_size=PAGE_SIZE_EPISODES):
        endpoint = self._endpoint(self.get_config()['services']['content']['client']['endpoints']['getDmcEpisodes']['href'], seasonId=season_id, pageSize=page_size, page=page)
        return self._json_call(endpoint)['data']['DmcEpisodes']

    def update_resume(self, media_id, fguid, playback_time):
        self._refresh_token()

        payload = [{
            'server': {
                'fguid': fguid,
                'mediaId': media_id,
                # 'origin': '',
                # 'host': '',
                # 'cdn': '',
                # 'cdnPolicyId': '',
            },
            'client': {
                'event': 'urn:bamtech:api:stream-sample',
                'timestamp': str(int(time()*1000)),
                'play_head': playback_time,
                # 'playback_session_id': str(uuid.uuid4()),
                # 'interaction_id': str(uuid.uuid4()),
                # 'bitrate': 4206,
            },
        }]

        endpoint = self.get_config()['services']['telemetry']['client']['endpoints']['postEvent']['href']
        return self._session.post(endpoint, json=payload).status_code

    def playback_data(self, playback_url):
        self._refresh_token()

        config = self.get_config()
        scenario = config['services']['media']['extras']['restrictedPlaybackScenario']

        if settings.getBool('wv_secure', False):
            #scenario = config['services']['media']['extras']['playbackScenarioDefault']
            scenario = 'tv-drm-ctr'

            if settings.getBool('h265', False):
                scenario += '-h265'

                if settings.getBool('dolby_vision', False):
                    scenario += '-dovi'
                elif settings.getBool('hdr10', False):
                    scenario += '-hdr10'

                if settings.getBool('dolby_atmos', False):
                    scenario += '-atmos'

        headers = {'accept': 'application/vnd.media-service+json; version=5', 'authorization': userdata.get('access_token'), 'x-dss-feature-filtering': 'true'}

        endpoint = playback_url.format(scenario=scenario)
        playback_data = self._session.get(endpoint, headers=headers).json()
        self._check_errors(playback_data)

        return playback_data

    def logout(self):
        userdata.delete('access_token')
        userdata.delete('expires')
        userdata.delete('refresh_token')

        mem_cache.delete('transaction_id')
        mem_cache.delete('config')
        mem_cache.delete('profile')

        self.new_session()
