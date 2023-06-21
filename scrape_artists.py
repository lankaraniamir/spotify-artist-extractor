# %%
import os
import sys
import copy
import json
import time
import httpx
import base64
import string
import asyncio
import requests
import itertools
import multiprocessing
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from collections import Counter
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
                 num_users=3, workers=multiprocessing.cpu_count()):
        """Initializes objects shared across threads and processes for scraping

        Args:
            parse_type -- whether to search through all genres for a given
            string when the isolated selection of a string without its later
            letters reaches the max allowable amount of 1000. Long will also
            allow for 3 word searches (default:quick, rapid, or long)
            update -- whether to search letters that have already been searched
            (default=false)
            self.scrape_num -- which number sp cookies to start with (default=0)
            num_users -- number of users' cookies in .env (default=3)
            workers -- how many cpu cores to use (default=cpu_count())
        """
        self.parse_type = parse_type
        self.update = update
        self.scrape_num = scrape_num
        self.num_users = num_users
        self.workers = workers

        self.SP_DCS = [os.getenv("SP_DC_" + str(i)) for i in range(num_users)]
        self.SP_KEYS = [os.getenv("SP_KEY_" + str(i)) for i in range(num_users)]

        self.VALID_CHARS = list(string.ascii_lowercase + string.digits + "_. ")
        self.VALID_START_CHARS = self.VALID_CHARS[:-2]
        self.ALPHANUMERICALS = self.VALID_CHARS[:-3]
        # self.PUNCTUATION = self.VALID_CHARS[-3:]
        # self.MAIN_AND = "and"
        self.SECONDARY_AND_WORDS = ["e", "en","et", "ja", "nd", "und", "y"]
        self.RELEVANT_AND_WORDS = self.SECONDARY_AND_WORDS

        if self.parse_type == "quick":
            self.MAX_WORDS = 4
        elif self.parse_type == "long":
            self.MAX_WORDS = 7
        elif self.parse_type == "rapid":
            self.MAX_WORDS = 2
        self.MAX_URL_LENGTH = 307

        self._setup_folders(parse_type)
        self.initialize_new_user()
        self.store_all_genres()

        self.client = None
        self.start_chars = ""
        self.all_artists = {}
        self.done_chars = {}

    async def get_query_results(self, cur_string):
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
        result_json = await self.get_url_result_json(query)

        total = result_json["total"]
        if total == 1000:
            children_sorted_by_amount, children_total = \
                await self.add_chars_to_query(cur_string)
            new_result_json = await self.get_url_result_json(
                f"""https://api.spotify.com/v1/search?q=artist:"{cur_string}"{
                    self.create_not_string(children_sorted_by_amount,
                    len(query), cur_string)}&type=artist&limit=50""")
            pure_total = new_result_json["total"]

            if pure_total > 0:
                await self.process_all_pages(result_json)
                if len(cur_string) <= 3:
                    print(f'"{cur_string}": {pure_total}')
            if pure_total == 1000 and self.parse_type == "long":
                await self.get_query_with_genre_results(children_sorted_by_amount,
                                                        cur_string)
            return total + children_total

        elif total > 0:
            await self.process_all_pages(result_json)
            if len(cur_string) <= 3:
                print(f'{cur_string}: {total}')
        return total

    async def add_chars_to_query(self, prior_string):
        """
        Calls for a search on each possible string containing the prior+1

        Takes a string and will find all valid next characters before calling
        for a search over each of those characters. It will also order the
        next characters by which ones result in the most artists for filtering
        the not queries. For space and period, we immediately skip a letter
        since these are meaningless at the end of a search string
        -- merged with get_cleaned functions
        """
        if len(prior_string) > 16:
            print(prior_string)

        # next_totals = {}
        next_chars = self.ALPHANUMERICALS.copy()
        words = prior_string.split(" ")
        cur_word = words[-1]

        if len(words) > 1:
            prev_word = words[-2]
            next_chars = [char for char in next_chars
                            if prev_word >= cur_word + char]

            # Debating below if elif
            if not cur_word:
                next_chars.remove(prev_word[0])
                if (len(words) <= 2 or prev_word[0] != words[-2][0]) \
                        and len(prev_word) > 1 and len(words) < self.MAX_WORDS:
                    await self.add_chars_to_query(prior_string + prev_word[0])
            elif prior_string[-2] == " " and len(prev_word) > 1 \
                    and cur_word[0] == prev_word[0]:
                next_chars.remove(prev_word[1])

            if cur_word and len(words) < self.MAX_WORDS \
                    and cur_word not in self.RELEVANT_AND_WORDS:
                await self.add_chars_to_query(prior_string + " ")
                # Debating below if
                if len(cur_word) > 1:
                    await self.get_query_results(prior_string + " " + cur_word)

        elif "." not in prior_string and "_" not in prior_string:
            await self.add_chars_to_query(prior_string + ".")
            await self.get_query_results(prior_string + "_")
            await self.add_chars_to_query(prior_string + " ")
            # Debating below
            await self.get_query_results(prior_string + " " + prior_string)
        elif (prior_string[-1] == "."):
            next_chars = filter(lambda i: i not in string.digits, next_chars)

        next_totals = {}
        for char in next_chars:
            # Debating below
            if cur_word + char not in self.RELEVANT_AND_WORDS:
                next_totals[char] = await self.get_query_results(
                                        prior_string + char)
            else:
                next_totals[char] = await self.add_chars_to_query(prior_string + char)

        children_sorted_by_amount = sorted(next_totals.keys(),
                                           key=lambda x: next_totals[x],
                                           reverse=True)


        return children_sorted_by_amount, sum(next_totals.values())

    async def get_query_with_genre_results(self, children_sorted_by_amount,
                                           cur_string):
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
                                }&type=artist&limit=50""")
            await self.process_all_pages(result_json)

    async def get_url_result_json(self, query_url,
                                            exception_num=0):
        """Sends POST to Spotify, extracts info, & handles errors & servers """
        try:
            result = await self._user.async_get(query_url, self.client)
        except Exception as e:
            exception_num += 1
            print(f"EXCEPTION {exception_num}: {e}\n{query_url}.")
            if exception_num >= 5:
                switch_server_and_user(self)
                return await self.get_url_result_json(query_url)
            else:
                time.sleep(.5)
                return await self.get_url_result_json(query_url, exception_num)

        if result.status_code == 200:
            return result.json()["artists"]
        elif result.status_code == 401:
            print(f"ERROR {result.status_code}: DNS Error - Switching VPN")
            switch_server_and_user(self)
            return await self.get_url_result_json(query_url)

        # Shouldn't occur in this implementation since doesn't use Spotify API
        elif (result.status_code == 429):
            print(f"ERROR {result.status_code}: Passed rate limit")
            wait_for_rate_limit(result)
            return await self.get_url_result_json(query_url)
        else:
            print(f"ERROR {result.status_code}: Other error occurred")
            time.sleep(.5)
            return await self.get_url_result_json(query_url)

    async def process_all_pages(self, result_json):
        """Extracts artists from every pages of results for the given query"""
        self.update_artists(result_json)
        while (result_json['next']):
            result_json = await self.get_url_result_json(
                result_json["next"])
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

    def initialize_new_user(self):
        """Makes connection token for current number user using .env cookies"""
        self._user = UserClient(sp_dc=self.SP_DCS[self.scrape_num % self.num_users],
                                sp_key=self.SP_KEYS[self.scrape_num % self.num_users])

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

        return len(full_artists)

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
def switch_server_and_user(scraper):
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
        if multiprocessing.current_process().name == "ForkProcess-1":
            print("Primary process is switching VPN & users")
            os.system("'/mnt/c/Program Files/NordVPN/nordvpn.exe' -c")
            fails = 0
            while not check_connection():
                time.sleep(1)
                fails += 1
                if fails > 12:
                    os.system("'/mnt/c/Program Files/NordVPN/nordvpn.exe' -c")
                    fails = 0
            print("New VPN connection established")
            scraper.scrape_num += 1
            scraper.initialize_new_user()
            print('Primary process connected as new user')
        else:
            time.sleep(5)
            while not check_connection():
                time.sleep(1)
            scraper.scrape_num += 1
            scraper.initialize_new_user()
            print("Secondary process connnected as new user")
    except Exception as e:
        print('*** Failure switching server and/or user for process '
                f'{multiprocessing.current_process().name}: e')


def check_connection():
    try:
        requests.get('http://www.google.com', timeout=1)
        return True
    except:
        return False


def wait_for_rate_limit(result):
    """Waits for the amount of time given by a Spotify rate limit"""
    wait_time = int(result.headers.get("Retry-After"))
    print(f"Rate limit exceeded. Please wait {wait_time} seconds.")
    time.sleep(wait_time)


# %%
async def start_async(scraper):
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
            if scraper.start_chars not in scraper.RELEVANT_AND_WORDS:
                await scraper.get_query_results(scraper.start_chars)
            else:
                # skip repeat querying and
                await scraper.add_chars_to_query(scraper.start_chars)
            print(f"Letters {scraper.start_chars} finished")
        except Exception as e:
            print(f"\n*** Failure processing {scraper.start_chars}: {e}\n")
            os._exit(e)

    try:
        with open(combo_file, 'w') as out_file:
            json.dump(dict(scraper.all_artists), out_file)

        with open(scraper.chars_file, "a") as out_file:
            out_file.write(f"{scraper.start_chars}: {len(scraper.all_artists)}\n")

        print(f"\nFinished dumping {scraper.start_chars} with cumulative " \
                f"{len(scraper.all_artists)} unique artists")
    except Exception as e:
        print(f"\n*** Failure writing results for {scraper.start_chars}: {e}\n")
        os._exit(e)


# %%
def start_batch(args):
    """Starts an async process for the given starting string & its process"""
    chars, scraper = args
    scraper.start_chars = chars
    # Debating below
    scraper.RELEVANT_AND_WORDS = [word for word in scraper.SECONDARY_AND_WORDS
                                  if word <= (chars + "z")]
    asyncio.run(start_async(scraper))

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
        scrape_num -- which user cookies to use (0 [default] to num_users)
        num_users -- number of users in environment variables (default=3)
        workers -- how many cpu cores to use (default=cpu_count())
        create_artists_file -- whether to create a full artists list prior to
        running the program (default=True)
    Example run:
        python3 scrape_artists.py quick False 1 3 15
    """
    parse_type = "quick" if len(sys.argv) > 1 and (sys.argv[1] == "quick" \
                    or sys.argv[1] == "short") else "long"
    update = True if len(sys.argv) > 2 and sys.argv[2] == "True" else False
    scrape_num = int(sys.argv[3]) \
                    if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0
    num_users = int(sys.argv[4]) \
                if len(sys.argv) > 4 and sys.argv[4].isdigit() else int(3)
    if len(sys.argv) > 5 and sys.argv[5].isdigit():
        scraper = ArtistScraper(parse_type, update, scrape_num,
                                num_users, int(sys.argv[5]))
    else:
        scraper = ArtistScraper(parse_type, update, scrape_num, num_users)

    with open(scraper.chars_file, "r") as in_file:
        finished = [combo.split(": ")[0] for combo in in_file.read().splitlines()]

    batches = [["".join(combo), scraper]
                for combo
                # in itertools.product(scraper.VALID_START_CHARS,
                                        # scraper.VALID_CHARS)
                in itertools.product(list(string.ascii_lowercase),
                                        list(string.ascii_lowercase))
                if scraper.update == True or "".join(combo) not in finished
                # remove 1-char and words from the batches
                and combo != "y " and combo != "e "
              ]

    print("\n")
    print(f"Running {scraper.parse_type} scrape w/ update={scraper.update} " \
          f"& self.scrape_num={scrape_num} "
          f"& self.workers={scraper.workers}")

    if len(sys.argv) <= 6 or sys.argv[6] == True:
        print("Unique artists found so far:",
        scraper.restore_full_artists_file())
    print("\n")

    with ProcessPoolExecutor(max_workers=scraper.workers) as executor:
        for artists in executor.map(start_batch, batches):
            print("A processor's execution fininshed")