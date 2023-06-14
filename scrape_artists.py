# %%
import os
import sys
import copy
import json
import time
import httpx
import base64
import ctypes
import string
import asyncio
import requests
import itertools
# import ujson as json
import multiprocessing
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor
load_dotenv()


# %%
class ArtistScraper(object):
    """
    Holds items for scraping Spotify common to all threads and processes.

    This class contains methods to extract all artists on Spotify while
    getting arond Spotify's rate limit. It's also rather extensible and can be
    altered to scrape other information. It assumes you call it from a method
    started in asyncio run, and it suppports multiprocessing to the extent your
    computer can handle. This function also assumes you are running on a WSL
    environment have NordVPN running as a regular Windows program, so that it
    can change VPN when Spotify begins rejecting scraping (only small changes
    are required to make this universal).

    Environment Variables:
        17 Spotify developer client IDS and secret keys must be included in a
        .env file numbered from 0 to 16 (eg SP_DC=0\nSP_KEY=0\nSP_DC=1...) also
        need to setup environment variables in a .env file in the same directory
        as this file. This .env should include 17 Spotify developer client IDs
        and secret keys. These can be acquired by creating spotify accounts in
        incognito windows, closing, and re-entering before checking the
        developer console for these values in the cookies for the current
        account. Do notlogout afterwards - simply exit.
    """

    def __init__(self, parse_type="short", update=False, scrape_num=0,
                 num_users=3, workers=multiprocessing.cpu_count()-1):
        """Initializes objects shared across threads and processes for scraping

        Args:
            parse_type -- whether to search through all genres for a given
            string when the isolated selection of a string without its later
            letters reaches the max allowable amount of 1000. Long will also
            allow for 3 word searches (default:quick, rapid, or long)
            update -- whether to search letters that have already been searched
            (default=false)
            scrape_num -- which number sp cookies to start with (default=0)
            num_users -- number of users' cookies in .env
            workers -- how many cpu cores to use
        """
        self.parse_type = parse_type
        self.update = update
        self.num_users = num_users
        self.workers = workers

        self.SP_DCS = [os.getenv("SP_DC_" + str(i)) for i in range(num_users)]
        self.SP_KEYS = [os.getenv("SP_KEY_" + str(i)) for i in range(num_users)]

        self.VALID_CHARS = list(string.ascii_lowercase + string.digits + "_. ")
        self.VALID_START_CHARS = self.VALID_CHARS[:-2]
        self.ALPHANUMERICALS = self.VALID_CHARS[:-3]
        self.PUNCTUATION = self.VALID_CHARS[-3:]
        self.COMMON_LETTERS = list('etaoinshrdlcumwfgypbvkjxqz _.')
        self.MAX_URL_LENGTH = 307

        self._setup_folders(parse_type)
        self.initialize_new_user(scrape_num)
        self.store_all_genres()

        self.client = None
        self.start_chars = ""
        self.all_artists = {}
        self.done_chars = {}

    async def get_query_results(self, cur_string, lock, scrape_num):
        """
        Calls current query, child queries, and maybe genre queries as needed

        If the current string's query reaches the max number of artists, we will
        call the query on each possible child string recursively providing both
        the strings unique to those substrings and also providing nots to
        requery the current string. If after excluding the popular next letters,
        it still reaches the max number of artists, we will call this selective
        search again but once for each of the 6000 genres on spotify. This
        should get most of the straggling artists.
        """
        query = f"""https://api.spotify.com/v1/search?q=artist:"{
                    cur_string}"&type=artist&limit=50"""
        result_json = await self.get_url_result_json(query, lock, scrape_num)

        total = result_json["total"]
        if total == 1000:
            children_sorted_by_amount, children_total = \
                await self.add_chars_to_query(cur_string, lock, scrape_num)
            new_result_json = await self.get_url_result_json(
                f"""https://api.spotify.com/v1/search?q=artist:"{cur_string}"{
                    self.create_not_string(children_sorted_by_amount,
                                            len(query),cur_string)
                    }&type=artist&limit=50""",
                lock, scrape_num)
            pure_total = new_result_json["total"]

            if pure_total > 0:
                await self.process_all_pages(result_json, lock, scrape_num)
                if len(cur_string) <= 3:
                    print(f'"{cur_string}": {pure_total}')
            if pure_total == 1000 and self.parse_type == "long":
                await self.get_query_with_genre_results(children_sorted_by_amount,
                                                        cur_string, lock, scrape_num)
            return total + children_total

        elif total > 0:
            await self.process_all_pages(result_json, lock, scrape_num)
            if len(cur_string) <= 3:
                print(f'{cur_string}: {total}')
        return total

    async def add_chars_to_query(self, prior_string, lock, scrape_num):
        """
        Calls for a search on each possible string containing the prior+1

        Takes a string and will find all valid next characters before calling
        for a search over each of those characters. It will also order the
        next characters by which ones result in the most artists for filtering
        the not queries. For space and period, we immediately skip a letter
        since these are meaningless at the end of a search string
        """
        if len(prior_string) > 12:
            print(prior_string)

        next_totals = {}
        # next_chars = await self.get_cleaned_alphanumericals(prior_string)
        next_chars = self.get_cleaned_alphanumericals(prior_string)

        if (prior_string[-1] == " " and prior_string[0] in next_chars):
            next_chars.remove(prior_string[0])
            await self.add_chars_to_query(prior_string + prior_string[0],
                                          lock, scrape_num)
        for char in next_chars:
            next_totals[char] = await self.get_query_results(
                                        prior_string + char, lock, scrape_num)

        children_sorted_by_amount = sorted(next_totals.keys(),
                                           key=lambda x: next_totals[x],
                                           reverse=True)

        if prior_string[-1] not in self.PUNCTUATION:
            skip_chars = self.get_cleaned_punctuation(prior_string)
            for char in skip_chars:
                if char == "_":
                    await self.get_query_results(prior_string + char,
                                                 lock, scrape_num)
                else:
                    await self.add_chars_to_query(
                    prior_string + char, lock, scrape_num)

        return children_sorted_by_amount, sum(next_totals.values())


    async def get_query_with_genre_results(self, children_sorted_by_amount,
                                           cur_string, lock, scrape_num):
        """
        Searches through each genre for artists w/ the current string

        This method is only used when the current string has reached the max
        number of results, 1000, even when excluding the most common strings
        containing one additional character.
        """
        base_query_length = len(cur_string) + 74
        for genre in self.genres:
            prior_length = base_query_length + len(genre)
            result_json = await self.get_url_result_json(
                f"""https://api.spotify.com/v1/search?q=genre:"{genre}"artist:"{
                    cur_string}"{self.create_not_string(children_sorted_by_amount,
                                                        prior_length, cur_string)
                    }&type=artist&limit=50""", lock, scrape_num)
            await self.process_all_pages(result_json, lock, scrape_num)

    async def get_url_result_json(self, query_url, lock, scrape_num,
                                            exception_num=0):
        """Sends POST to Spotify, extracts info, & handles errors & servers """
        try:
            result = await self._user.async_get(query_url, self.client)
        except Exception as e:
            exception_num += 1
            print(f"EXCEPTION {exception_num}: {e}\n{query_url}.")
            if exception_num >= 5:
                switch_server_and_user(self, lock, scrape_num)
                return await self.get_url_result_json(query_url, lock,
                                                      scrape_num)
            else:
                time.sleep(.5)
                return await self.get_url_result_json(query_url, lock,
                                                      scrape_num, exception_num)

        if result.status_code == 200:
            return result.json()["artists"]
        elif result.status_code == 401:
            print(f"ERROR {result.status_code}: DNS Error - Switching VPN")
            switch_server_and_user(self, lock, scrape_num)
            return await self.get_url_result_json(query_url, lock, scrape_num)

        # Shouldn't occur in this implementation since doesn't use Spotify API
        elif (result.status_code == 429):
            print(f"ERROR {result.status_code}: Passed rate limit")
            await wait_for_rate_limit(result)
            return await self.get_url_result_json(query_url, lock, scrape_num)
        else:
            print(f"ERROR {result.status_code}: Other error occurred")
            time.sleep(.5)
            return await self.get_url_result_json(query_url, lock, scrape_num)

    async def process_all_pages(self, result_json, lock, scrape_num):
        """Extracts artists from every pages of results for the given query"""
        self.update_artists(result_json)
        while (result_json['next']):
            result_json = await self.get_url_result_json(
                result_json["next"], lock, scrape_num)
            self.update_artists(result_json)

    def create_not_string(self, children_sorted_by_amount,
                          prior_length, cur_string):
        """
        Creates string of most common artist bases to exclude from search

        Will go through each possible substring that can be made by adding
        a character to the current substring, prioritizing different characters
        by the number of artists they result in. It will then add each to a
        string separated by NOT operators until the total length of the string
        is longer than the max Spotify allows. This results in reducing the
        number of results when a search has the max results, 1000.
        """
        not_string = ""
        for char in children_sorted_by_amount:
            if (prior_length + len(not_string) + len(cur_string) + 1
                    <= self.MAX_URL_LENGTH):
                break
            not_string += f''' NOT artist:"{cur_string + char}"'''
        return not_string

    def get_cleaned_alphanumericals(self, prior_string):
        """
        Prevents repeating chars and unnecessary search letters & numbers

        The first if prevents having a one-letter word where its first letter is
        immediately repeated in the next word making the first word irrelevant
        and repeating the same search.
        Second if prevents starting a word with the same letter as the first
        word so it just removes this letter and searches the skip of this letter.
        Third, if prevents having two words with the same starting two
        letters repeating the same letter twice in a row.
        """
        next_chars = self.ALPHANUMERICALS.copy()
        words = prior_string.split(" ")
        if (len(prior_string) == 2 and prior_string[1] == " "
                and prior_string[0] in next_chars):
            next_chars.remove(prior_string[0])
        elif (prior_string[-1] == "."):
            next_chars = filter(lambda i: i not in string.digits, next_chars)
        if len(words) > 1:
            next_chars = [char for char in next_chars
                            if words[-2] >= words[-1] + char]
            if (prior_string[-2] == " "):
                for word in words[:-1]:
                    if (len(word) >= 2 and prior_string[-1] == word[0]
                        and word[1] in next_chars):
                        next_chars.remove(word[1])
        return next_chars

    async def get_cleaned_words(self, prior_string):
        """
        TODO: Create more comprehensive word-based quering system that better
        searches through different word combinations and skips over duplicated
        word combos by only allowing alphabetical combinations (as Spotify
        defines words w/out location) - not production ready yet but added some
        techniques like the alphabetic sorting to the method above temprarily
        """
        next_chars = self.ALPHANUMERICALS.copy()
        words = prior_string.split(" ")
        # first_word_in_alphabetical = sorted(words)[0]

        if len(words) > 1:
            if prior_string[-1] == " ":
                for word in words:
                    if len(word) == 1 and word[0] in next_chars:
                        next_chars.remove(word[0])
                words = words[:-1]

            current_word = words[-1]

            for word in words[:-1]:
                if (len(word) > len(current_word)
                        and word[:len(current_word)] == current_word
                        and word[len(current_word)] in next_chars):
                    next_chars.remove(word[len(current_word)])
                    if len(current_word) == 1:
                        await self.add_chars_to_query(prior_string +
                                                    word[len(current_word)],
                                                        lock, scrape_num)

            first_letters = [key for (key, val) in
                        Counter([word[0] for word in words]).items() if val > 1]
            for letter in first_letters:
                if letter in next_chars:
                    next_chars.remove(letter)

            for char in next_chars:
                # if first_word_in_alphabetical < current_word + char:
                if words[-2] < current_word + char:
                    next_chars.remove(char)
        return next_chars


    def get_cleaned_punctuation(self, prior_string):
        """
        Prevents punctuation cycles and removes unuseful punctuation

        Based upon my experimentation, underscore, period, and space are the
        only punctuation that result in unique combinations when used as search
        terms. However, period and space only work if not at start of word. "/"
        and probably "|" also work as search terms but result in separating the
        start of two different words which isn't necessary for this method.
        Also, none of the punctuation can end a search query so we immediate go
        to next character in these situations.
        Through experimentation and basic logic, I found out that there won't be
        unique strings that have two unique punctuationsthat
        wouldn't already be reflected in the results of having one of those
        punctuation. This also means that an additional word won't help us
        isolate punctuation already found.
        Furthermore, the utility of spaces remains slightly even after 2 spaces
        (eg bhai ji singh > 100) but the spaces multiply the computation time
        for only a few extra artists provided so we only explore a large subset
        of spaces when doing a long search as these artists would almost always
        already be predicted by one or two words.
        """
        skip_chars = self.PUNCTUATION.copy()
        space_count = prior_string.count(" ")
        if "." in prior_string or "_" in prior_string:
            skip_chars.remove(" ")
            skip_chars.remove(".")
            skip_chars.remove("_")
        elif space_count >= 1:
            skip_chars.remove(".")
            skip_chars.remove("_")
            if ((space_count >= 6 and self.parse_type == "long")
                    or (space_count >= 2 and self.parse_type == "quick")
                    or (self.parse_type == "rapid") or prior_string[-2] == " "):
                skip_chars.remove(" ")

        return skip_chars

    def remove_irrelevant_traits(self, result_json):
        """Keeps only desired info on the artists & removes duplicates"""
        artists = {
            artist["id"]: {
                "name": artist["name"],
                "genres": artist["genres"],
                "popularity": artist["popularity"]
            }
            for artist in result_json["items"]
            if artist is not None and artist["id"] not in self.all_artists.keys()
        }
        return artists

    def update_artists(self, result_json):
        """Adds new artists to the all_artists dictionary after cleaning"""
        return self.all_artists.update(self.remove_irrelevant_traits(result_json))

    def initialize_new_user(self, scrape_num):
        """Makes connection token for current number user using .env cookies"""
        self._user = UserClient(sp_dc=self.SP_DCS[scrape_num % self.num_users],
                                sp_key=self.SP_KEYS[scrape_num % self.num_users])

    def store_all_genres(self):
        """Gets all genres that exist on Spotify from Everynoise

        Extract all genres that have ever been on Spotify. Genres provided by
        Spotify are only a very small subset so extracts all the genres from
        the Every Noise at Once project by a Spotify employee, Glenn McDonald,
        based on Spotify's internal API
        """
        link = "https://everynoise.com/everynoise1d.cgi?scope=all"
        html = requests.get(link).text
        soup = BeautifulSoup(html, "lxml")
        tds = soup.find_all("td", {"class": "note"})
        genres = [line.contents[0].contents[0] for line in tds[1::2]]
        self.genres = copy.deepcopy(genres)

    def restore_full_artists_file(self):
        """Quickly recreates full_artist file from all combo_artist files"""
        full_artists = {}
        for file in os.listdir(self.combo_dir):
            combo_path = os.path.join(self.combo_dir, file)
            if os.path.isdir(combo_path):
                continue
            with open(self.combo_dir + file, "r") as in_file:
                full_artists.update(dict(json.load(in_file)))

        if (os.path.exists(self.artists_file)
                    and not os.stat(self.artists_file).st_size == 0):
            with open(self.artists_file, 'r') as in_file:
                full_artists.update(dict(json.load(in_file)))

        with open(self.artists_file, 'w') as out_file:
            json.dump(dict(full_artists), out_file)
            # ujson.dump(dict(full_artists), out_file, ensure_ascii=False, indent=4)

    def _setup_folders(self, parse_type):
        """Creates files/folders for storing results if don't already exist"""
        self.results_dir = parse_type + "_results/"
        if not os.path.isdir(self.results_dir):
            os.mkdir(self.results_dir)
        self.combo_dir = os.path.join(self.results_dir, "combo_results/")
        if not os.path.isdir(self.combo_dir):
            os.mkdir(self.combo_dir)

        self.artists_file = os.path.join(self.results_dir, "full_artists.json")
        if (not os.path.exists(self.artists_file)
                    or os.stat(self.artists_file).st_size == 0):
            with open(self.artists_file, "w") as out_file:
                json.dump({}, out_file)
        self.chars_file = os.path.join(self.results_dir, "done_chars.txt")
        if not os.path.exists(self.chars_file):
            with open(self.chars_file, "w") as out_file:
                pass


# %%
class ArtistScraperAPI(ArtistScraper):
    """
    Version of Artist Scraper scraping by API instead of by user -- deprecated

    This class is virtually the same as its parent class but it uses the Spotify
    API. This resulted in reaching rate-limits and slowdowns which makes it
    overall significantly worse than the other method. This class is kept in
    case the prior method gets patched out. It no longer works with many parent
    methods as it uses a different client base but by simply changing the
    clients to the attphttpx async client, it should work in most of the
    functions. Requries client id and client secret in the dot env.
    """
    def __init__(self, parse_type="long", update=False,
                 workers=multiprocessing.cpu_count()-1,
                 sync_client=None, async_client=None):

        self.VALID_CHARS = list(string.ascii_lowercase + string.digits + "_. ")
        self.VALID_START_CHARS = self.VALID_CHARS[:-2]
        self.ALPHANUMERICALS = self.VALID_CHARS[:-3]
        self.PUNCTUATION = self.VALID_CHARS[-3:]
        self.COMMON_LETTERS = list('etaoinshrdlcumwfgypbvkjxqz _.')
        self.MAX_URL_LENGTH = 307

        self._setup_folders(parse_type)
        self.store_all_genres()

        self.sync_client = sync_client
        self.async_client = async_client
        self.start_chars = ""
        self.all_artists = {}
        self.done_chars = {}

        self.parse_type = parse_type
        self.update = update
        self.workers = workers

        self.token = self.get_api_token()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def get_api_token():
        """Gets normal access token for regular API requests"""
        client_id = os.getenv("CLIENT_ID")
        client_secret = os.getenv("CLIENT_SECRET")
        message = f"{client_id}:{client_secret}"
        url = "https://accounts.spotify.com/api/token"

        message_bytes = message.encode("utf-8")
        base64_bytes = base64.b64encode(message_bytes)
        base64_message = base64_bytes.decode("utf-8")
        data = {"grant_type": "client_credentials"}
        headers = {
            "Authorization": f"Basic {base64_message}",
            "Content-Type": "Application/x-www-form-urlencoded"
        }

        result = requests.post(url, data=data, headers=headers)
        token = result.json()["access_token"]
        return token

    async def get_url_result(self, query_url):
        """Accesses the URL with the given cookies and returns its data"""
        result = await self.async_client.get(query_url, self.headers,
                                             self.async_client)

    def store_some_genres(self, headers):
        """Gets a subset of genres Spotify shares publicly"""
        query_url = \
            f"https://api.spotify.com/v1/recommendations/available-genre-seeds"
        result = requests.get(query_url, headers=headers)
        self.some_genres = result.json()["genres"]


class UserClient(object):
    """
    Simulates a profile & client allowing connections & queries outside the API

    This class is a minor modification of the SpotifyClient class from the
    open-source SpotiFile project. It allows the program to act on a user
    profile, given its cookies, allowing us to extract queries without any API
    thus bypassing the rate limit. It creates a request session that allows for
    the cookies and headers to remain persistent, while allowing the same TCP
    connection to be reused for many requests.

    This class was modified to support concurrency, remove a lot of overhead and
    extraneous processing unuseful to this project, and  allow for the use of
    multiple users
    """
    def __init__(self, sp_dc=None, sp_key=None):
        """Sets up user info given cookies and headers"""
        self.sp_dc = os.getenv("SP_DC_0") if sp_dc is None else sp_dc
        self.sp_key = os.getenv("SP_KEY_0") if sp_key is None else sp_key

        self._verify_ssl = True
        self.__USER_AGENT = 'Mozilla/5.0'
        self.__HEADERS = {
                'User-Agent': self.__USER_AGENT,
                'Accept': 'application/json',
                'Origin': 'https://open.spotify.com',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'Referer': 'https://open.spotify.com/',
                'Te': 'trailers',
                'App-Platform': 'WebPlayer'
            }
        self.initialize_tokens()
        print("Successfully initialized user connection!")

    async def async_get(self, query_url, async_client):
        """Does a query as if we were a user on Spotify in a shared session"""
        with requests.session() as session:
            session.headers = self.__HEADERS
            session.headers.update({
                                    'Client-Token': self._client_token,
                                    'Authorization': f'Bearer {self._access_token}'
                                    })

            response = await async_client.get(query_url, headers=session.headers)
            return response

    def initialize_tokens(self):
        """Begins a persistent session by getting access & client tokens"""
        with requests.session() as session:
            session.headers = self.__HEADERS
            cookies = {"sp_dc": self.sp_dc, "sp_key": self.sp_key}
            try:
                result_json = session.get(
                        'https://open.spotify.com/get_access_token',
                         verify=self._verify_ssl, cookies=cookies).json()
                self.is_anonymous = result_json['isAnonymous']
            except Exception as ex:
                print('An error occured when generating an access token!', ex,
                      "\n This probably means the account was deleted.")
                exit(0)
            self._access_token = result_json['accessToken']
            self._client_id = result_json['clientId'] \
                    if result_json['clientId'].lower() != 'unknown' \
                    else self._client_id

            if 'client_token' in session.headers:
                session.headers.pop('client_token')
            if 'Authorization' in session.headers:
                session.headers.pop('Authorization')
            data = {
                "client_data": {
                    "client_version": "1.2.13.477.ga4363038",
                    "client_id": self._client_id,
                    "js_sdk_data":
                    {
                        "device_brand": "",
                        "device_id": "",
                        "device_model": "",
                        "device_type": "",
                        "os": "",
                        "os_version": ""
                    }
                }
            }
            response_json = session.post(
            'https://clienttoken.spotify.com/v1/clienttoken',
            json=data, verify=self._verify_ssl).json()
            self._client_token = response_json['granted_token']['token']


# %%
def switch_server_and_user(scraper, lock, scrape_num):
    """
    Switches to a new server and user whenever the old server has been blocked

    The first process to be blocked will grab the mutex & sleep an ample amount
    of time to allow all other processes to get locked. Then, since they are all
    no longer requesting, it will call on the NordVPN Windows program to switch
    servers and it will wait for the vpn to properly switch. It will then switch
    to the next available user to further secure anonymity. Meanwhile, all other
    processes will skip this non-blocking lock before waiting on the same lock
    in a blocking manner in an else statement that won't result in switching the
    VPN again. Once the first process finishes, it will release the lock
    allowing all other processes to grab and release the lock before
    initializing its next user. The initial sleep prevents the program from not
    grabbing the lock while a process is writing to file
    """
    try:
        time.sleep(8)
        if lock.acquire(blocking=False):
            try:
                os.system("'/mnt/c/Program Files/NordVPN/nordvpn.exe' -c")
                time.sleep(8)
                scrape_num.value += 1
                scraper.initialize_new_user(scrape_num.value)
                print('main lock succeeded')
            except Exception as e:
                print("*** Primary sleeping failed:", e)
            finally:
                lock.release()
        else:
            try:
                lock.acquire()
                lock.release()
                scraper.initialize_new_user(scrape_num.value)
            except:
                print('*** Secondary locks failed', e)
    except Exception as e:
        print('*** Initial locking in switching servers failed:', e)



async def wait_for_rate_limit(result):
    """Waits for the amount of time given by a Spotify rate limit"""
    wait_time = int(result.headers.get("Retry-After"))
    print(f"Rate limit exceeded. Please wait {wait_time} seconds.")
    await asyncio.sleep(wait_time)
    # time.sleep(wait_time)


# @contextmanager
# def nonblocking(lock):
#     """
#     Allows nonblocking locks to work in a way that can avoid race conditions

#     copied from stack overflow post:
#     https://stackoverflow.com/questions/31501487/non-blocking-lock-with-with-statement
#     """
#     locked = lock.acquire(blocking=False)
#     try:
#         yield lock
#     finally:
#         if locked:
#             lock.release()


# %%
async def start_async(scraper, lock, write_lock, scrape_num, full_artists):
    """
    Starts the async scraping process & writes out updated results

    Must be called by asyncio.run with an intialized scraper to work. This will
    setup the dictionary for the current process, call for the process to run,
    combine the process results with the overall dictionary, and will then write
    out the results to the same files it initially read from. If a 2 or more
    letter string is provided, it will query all possible subqueries starting
    at the provided string. However, 1 letter string is far too many queries for
    one async process so it will instead will only check the genres for that one
    letter excluding the most popular second letter combination; this will only
    occur on the quick parsing option. Asyncness here is pivotal as it allows
    processing to continue at each request for each processor
    """
    async with httpx.AsyncClient() as client:
        scraper.client = client
        print(f"starting {scraper.start_chars}")

        combo_file = os.path.join(scraper.combo_dir,
                                        scraper.start_chars + '_artists.json')
        if scraper.update and os.path.exists(combo_file):
            with open(combo_file, 'r') as in_file:
                scraper.all_artists = dict(json.load(in_file))
        else:
            scraper.all_artists = {}

        try:
            if len(scraper.start_chars) > 1:
                await scraper.get_query_results(scraper.start_chars,
                                                    lock, scrape_num)
            else:
                with open(scraper.chars_file, 'r') as in_file:
                    rel_strings = [combo.split(": ") for combo
                                    in in_file.read().splitlines()
                                    if combo.split(": ")[0][0]
                                        == scraper.start_chars]
                rel_strings.sort(key=lambda x: int(x[1]), reverse=True)
                await scraper.get_query_with_genre_results(rel_strings,
                    scraper.start_chars, lock, scrape_num)

            print(f"Letters {scraper.start_chars} finished")
        except Exception as e:
            print(f"\n***Failure at {scraper.start_chars}: {e}\n")
            os._exit(e)

    try:
        with open(combo_file, 'w') as out_file:
            json.dump(dict(scraper.all_artists), out_file)

        full_artists.update(scraper.all_artists)

        # with nonblocking(write_lock) as locked:
        if write_lock.acquire(blocking=False):
            print("Writing full_artists for", scraper.start_chars)
            with open("quick_results/full_artists.json", 'w') as out_file:
                json.dump(dict(full_artists), out_file)
            print("Finished writing full_artists for", scraper.start_chars)
        else:
            print("Skipping write for", {scraper.start_chars})

        with open(scraper.chars_file, "a") as out_file:
            out_file.write(f"{scraper.start_chars}: {len(scraper.all_artists)}\n")

        print(f"\nFinished dumping {scraper.start_chars} with cumulative " \
                f"{len(scraper.all_artists)} unique artists resulting in " \
                f"{len(full_artists)} artists total\n")
    except Exception as e:
        print(f"***Failure at writing results for {scraper.start_chars}: {e}")


# %%
def start_batch(args):
    """Starts an async process for the given starting string & its process"""
    # print(os.getpid())
    chars, scraper, lock, write_lock, scrape_num, full_artists = args
    scraper.start_chars = chars
    try:
        asyncio.run(start_async(scraper, lock, write_lock, scrape_num, full_artists))
    except Exception as e:
        print("Exception:", e)


# %%
if __name__ == "__main__":
    """
    Inits data & triggers each processor to query a starting string batch

    Creates variables to be shared across all processes using a manager; these
    include a lock to prevent multiple processes from switching VPN at once, a
    dictionary of artists to keep an update list of all possible artists all at
    once, and a number for the current user to use. All other data will be
    copied into each subprocess and so they are included in the class instead.
    All parser arguments here are optional and are the same as the ArtistScraper
    class. After initializing all possible 1 or 2 letter starting strings
    (excluding already processed ones if update is set to false), this will then
    consider each string a separate batch and a separate process will work on a
    single starting string at a time before going on to the next available
    starting string. This is combined with the async processing to make each
    processor constantly work.

    Arguments: (optional)
        parse_type -- whether to search through all genres for a given string
        ("quick" [default], "rapid", or "long")
        update -- whether to search letters that have already been searched
        (False [default] or True)
        scrape_num -- which user cookies to use (0 [default] to 17)
        num_users -- number of users in environment variables (default=3)
        workers -- how many cpu cores to use (default=cpu_count()-1)

    Example run:
        python3 scrape_artists.py quick False 1 3 15
    """
    manager = multiprocessing.Manager()
    lock = manager.Lock()
    write_lock = manager.Lock()

    parse_type = "quick" if len(sys.argv) > 1 and (sys.argv[1] == "quick" \
                    or sys.argv[1] == "short") else "long"
    update = True if len(sys.argv) > 2 and sys.argv[2] == "True" else False
    scrape_num = manager.Value(ctypes.c_int, int(sys.argv[3])) \
                    if len(sys.argv) > 3 and sys.argv[3].isdigit() \
                    else manager.Value(ctypes.c_int, 0)
    num_users = int(sys.argv[4]) \
                if len(sys.argv) > 4 and sys.argv[4].isdigit() else int(3)
    if len(sys.argv) > 5 and sys.argv[5].isdigit():
        scraper = ArtistScraper(parse_type, update, scrape_num.value,
                                num_users, int(sys.argv[5]))
    else:
        scraper = ArtistScraper(parse_type, update, scrape_num.value, num_users)

    scraper.restore_full_artists_file()

    with open(scraper.artists_file, "r") as in_file:
        full_artists = manager.dict(json.load(in_file))

    with open(scraper.chars_file, "r") as in_file:
        finished = [combo.split(": ")[0] for combo in in_file.read().splitlines()]
    batches = [["".join(combo), scraper, lock, write_lock,
                                scrape_num, full_artists]
                for combo
                in itertools.product(scraper.VALID_START_CHARS,
                                        scraper.VALID_CHARS)
                if (scraper.parse_type == "long" or len(combo) == 2)
                and (scraper.update == True or "".join(combo) not in finished)]

    print("\n")
    print(f"Running {scraper.parse_type} scrape w/ update={scraper.update} " \
          f"& scrape_num={scrape_num.value} "
          f"& self.workers={scraper.workers}")
    print("Unique artists found so far:", len(full_artists))
    print("\n")

    with ProcessPoolExecutor(max_workers=scraper.workers) as executor:
        for artists in executor.map(start_batch, batches):
            print("A processor's execution fininshed")

    manager.shutdown()

