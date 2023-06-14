# Spotify Artist Extractor


## Approach

This program uses Spotify's web API search query functionality to find artists based upon the characters comprising the starts of their names. More specifically, it iterates through each possible character valid within the Spotify search API and extracts all artists whose names contain any words that starts with those characters. If there are over 1000 artists meeting the current search critera (the max number extractable within the Web API), it will recursively repeat this process by adding all possible next characters (eliminating to the current string querying all artists that match this new string. This process repeats until reaching a string which defines less than 1000 artists. After all strings adding a character to a parent string that are returned, it will attempt to search this parent string again but this time excluding the child strings that define the largest number of artists thereby pruning this search to return a smaller subset of artists finding new artists that were excluded by the 1000 limit. If at this point, this string's query still returns 1000 artists, the program will further prune the artists with the given string and given exclusions by testing this string and exclusion pattern for all 6213 genres used by Spotify which in most cases will prune out many remaining artists with these strings as the associations between names tend to be varied so it will often result in these strings having less than 1000 in practically every genre; however, the benefits of this aren't too extensive and this results in a big computation increase so this process is only done if the long parse_type selection is chosen. As each two-character starting string finishes its recursive process, its values are added to a global dictionary of all artists found so far and both the dictionary for this starting string and the global dictionary are outputted to a file.


## Speed Considerations

As there are about 40 valid characters within the Spotify search API, each letter added to a string results in additional 40 terms being searched on the Spotify web API. This can quickly balloon at larger depths as times as it is an exponential increase with a base of 40 resulting in a large O(40^h) depth based upon the length of the leaf strings. This problem is further exacerbated by the response request functionality which requires each process to wait for Spotify to get the query message and for Spotify to send a response message. Thus, several method swere used to improve this timing. First, a ProcessPoolExecutor is used to ensure that all processors available are getting used at all times by having each processor focus on a different two-letter string to minimize overlap and conflicts. Furthermore, we don't want our processes to be stoppin whenever we have to make an HTML request or do some lengthy process, so we use the async  and iohttpx libraries to set up an asynchronous environment where each processor can divert its attentions to task as certain other tasks are completed. Both of these two processes showed masssive improvements in the speed at which the program ran, especially when used in tandem, even though these two libraries weren't initially designed to be combined in this way, and it allows the program to scale to even higher speeds on better systems. In order to keep all processes and their async operations in sync, we use a manager to share a lock, the full dictinoary, and the user number between the processes; all other variables are duplicated for each process and kept locally. Additionally, each process will only update the full file after it finishes a single batch and will write the full dictionary to the file only if another process isn't already, as ensured by a writer lock, since the writing process can take several minutes for larger jsons. Lastly, various word-processing techniques are used to eliminate unecessary search terms such as only searching terms in alphabetical order so that no word combination is repeated.


## Bypassing the rate limit

While the API is useful, its rate limit is often reached within only a few minutes on this code, and the wait time is typically about 20 hours long. In order to bypass this, we use a modified version of the client functionality from [Spotifile](https://github.com/Michael-K-Stein/SpotiFile) adapted to be usable in an async, multiprocessing enviornment. This functionality uses a user's browser cookies to access the Spotify web player as if we were that user exploring music instead of as if we were a program using the web API, thus allowing us to continue our extraction process without long wait periods. It also uses a client session so that multiple TCP connections can become bundled under a single session limiting the number of connections required significantly.  Every once in awhile (but significantly rarer than the rate limit), Spotify will start signalling DNS errors likely due to overuse of a single IP and user, so in these cases we lock all processes and have a single process change our VPN connection and our user before all processes then create a new connection to this new user at the current IP. It then continues the main event loop. The more users you have the more extensive this process can be, but I've found 2 or 3 users is already enough to cycle around; these are not shared publicly for privacy reasons but can be proviced by request.


## Setting up an Environment

In order to download all packages necessary for this program, you must install
mamba or conda and use it to load and activate the environment from the provided
yaml file as follows:

```
git clone https://github.com/lankaraniamir/Spotify-Artist-Extractor
cd https://github.com/lankaraniamir/Spotify-Artist-Extractor
conda env create -f environment.yml
conda activate artist_scraper
```

This script also assumes you are operating in a WSL environment and have a copy of NordVPN on Windows itself; however, it can be easily modified to work for any other system as well. In the switch_server_and_user function, there is a line that calls for the Nord VPN to reconnect from the perspective of my WSL environment shell, but this can easily work in any other command line if it is changed to reflect the location of your NordVPN copy, if you have one, and your operating system's langauge. Any other VPN or proxy can also be used as long as you either actively stop and change the VPN location every once in awhile or replace this line with code to operate your VPN.

In order for the script to properly function, the proper environment variables
must also be set in a .env file. This includes an SP_DC_#={} and SP_KEY_#={} for
each user account you are using for this process. This can be done by creating
an account in an incognito window, exiting the window without logging out,
reopening the account in a new incognito window, and using the developer console
to extract the SP_DC and SP_KEY strings; for more details look in the readme of
[Spotifile](https://github.com/Michael-K-Stein/SpotiFile).


## How to Run

Once the environment is fully prepared, this script can run fully using solely the scrape_artists.py file which holds all classes and functions as well as the main method. Running with all the default parameters is simply:
```
python3 scrape_artists
```
However, several attributes can be modified depending on what type of search you
want.

* Parse_type: whether to include genre searches and how many spaces to allow. The default, "quick" will not do genre searches and will allow 2 spaces. The more comprehensive 'long' search decides if we should search through all genres for a given string if the string still has 1000 results after excluding child strings and will limit the number of words allowed at 6. And the fastest option "rapid" will not do genre search and will only allow 1 space.

* Update: whether to search through combinations that have already finished exploring in order to potentially find more artists within this combination. True will repeat all combinations. False will only search string combinations that have not finished searching yet.

* Scrape_num: Which number user as reflected by the environment variables to
  start with

* Num_users: Number of users' info held by your .env file

*  Workers: how many CPU cores to dedicate to the multiprocessing unit. Note that the manager function will add additional processing overhead to account for so it defaults to number of cores minus 1.

For example, the following code will scrape artists using an expanded approach
including genres, repeating searches of those sections already searched,
assuming we are starting with 0 users out of 3, and want the multiprocessor to
use 15 processes:

```
python scrape_artists long true 0 3 15
```

All artists encoded by a 2 letter starting string will be stored in a results
folder based upon the parse_type for that combination. The combination of all
artists found by all start strings, without duplications, can be found in the
full_artists file. Done_chars stores the history of all 2-letter combinations
you've searched and the number of artists found in that search. I have included
the combination string results when run on a little less than 3 unique starting
characters as a representation of what the process looks like (already 2 million
artists extracted) but Github won't allow me to upload the file of all artists
since it is too big. Either way, the program still needs to be run nonstop for awhile to
finish extracting all the artists.

