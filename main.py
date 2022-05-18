import asyncio
import audioop
import copy
import datetime
import os
import re
from string import punctuation

import pyaudio
import pygame
from deepgram import Deepgram
from deepgram.transcription import LiveTranscription


# Data structure to help associate words with their timestamps.
# We display Deepgram's transcription, but we need to track the individual words to make old lines scroll away.
# Note that the words' text DOES NOT necessarily match the transcription - that has capitalization, punctuation, etc.
class TranscriptionWord:
    equality_tolerance = 0.01  # If two offset times are within this many seconds, they're equal enough for me

    def __init__(self, text, start, end, time_offset=0, request_id=0):
        self.text = text
        self.start = start  # Note that Deepgram's API returns times in seconds since the beginning of the snippet.
        self.end = end
        self.time_offset = time_offset  # We're stringing multiple snippets into one, so we need a global time offset.
        self.request_id = request_id  # Used to confirm that Deepgram's API doesn't juggle multiple interim transcripts
        # It would be an absolute nightmare to code for that, so I want to make sure it's a real possibility.

    # Number of seconds between the time this whole set of transcripts began, and the time this word began
    def get_offset_start(self):
        return self.start + self.time_offset

    # Number of seconds between the time this whole set of transcripts began, and the time this word ended
    def get_offset_end(self):
        return self.end + self.time_offset

    # Convenience method to apply tolerance in comparing timestamps.
    # Deepgram's temporal subdivision in interim transcripts is quite jittery - all word bounds shift every time.
    def _roughly_equals(self, a, b):
        return abs(a - b) <= self.equality_tolerance

    # Compares timestamps to see if this word and another word could represent the same point in time.
    # Used to match expired words that have already been shown, when the interim transcript is super long.
    def coincides_with(self, other):
        return (self._roughly_equals(self.get_offset_start(), other.get_offset_start()) or
                self.get_offset_start() > other.get_offset_start()) \
               and \
               (self._roughly_equals(self.get_offset_end(), other.get_offset_end()) or
                self.get_offset_end() < other.get_offset_end())

    # Convenience method to check if this abstract word-object matches up to a linguistic word on a transcript.
    # Punctuation and capitalization and stuff are applied to the transcript but not the word text.
    def represents(self, stringerino):
        stripped = stringerino.strip(punctuation)
        if not len(stripped):
            print(f'WARNING: String {stringerino} was all punctuation. Probably gonna cause problems')
        return self.text.casefold() == stripped.casefold()

    # Convenience method to cull words before a threshold - IE, the last time we blew a line off the display.
    def ended_before(self, offset):
        return self.end + self.time_offset < offset


class SubtitleDisplay:
    word_discard_pile: list[TranscriptionWord]
    finalized_words: list[TranscriptionWord]
    interim_words: list[TranscriptionWord]
    currently_displayed_words: list[list[TranscriptionWord]]

    def __init__(self, display, font):
        self.display = display
        self.font = font
        self.font_height = self.font.size("Tg")[1]

        self.done = False

        '''
        All this baloney is here because we need to scroll the captions along, whether or not they're final.
        IE, if I say "four score and seven years ago, Abraham Lincoln said some stuff and I forgot most of it"
        it won't fit on the shirt. We need to display each line for a bit, then move on.
        The problem is that we need to use interim mode for responsiveness, so parts of the transcript will change.
        By carefully tracking the individual words as well as the transcripts, we can use timestamps to
        figure out which SOUNDS have been shown to the viewer, so we don't end up showing the same WORDS repeatedly.  
        '''
        self.minimum_line_display_time = 0.75  # Minimum number of seconds that each word should display on the top line
        self.timebase = None  # Datetime when first word of this caption set was displayed
        self.request_time_offsets = {}  # Seconds since timebase when each batch of words started arriving
        self.line_expiration_start_time = datetime.datetime.min  # Datetime when the previous top line expired
        self.word_discard_pile = []  # These words have been shown already. Even if they change, it's too late.
        self.finalized_transcript = ''  # Finalized transcripts will not change.
        self.finalized_words = []       # No matter what interim bullhonkey takes place, it can miss these.
        self.interim_transcript = ''    # Interim transcripts change constantly. I clear these when they become final.
        self.interim_words = []         # This collection is blown out every time new data arrives and recreated.
        self.current_request_id = -1    # We use this to offset the timestamps of interim data.
        '''
        We also need to track what's being SHOWN ON MY CHEST, too!
            - If the top line changes, we need to reset the timer to let the viewer catch up.
            - When the line display time elapses, we'll use this to figure out which words are FRICKIN DEAD.
        '''
        self.currently_displayed_words = []  # Current TranscriptionWords being shown onscreen, one list per line.

        self.unprocessed_transcription_queue = None     # Depositing transcripts and interpreting them are separate ops.
        self.transcript_lock = asyncio.Lock()           # Don't mess with the data while we're using em!

    def start_the_loops_brother(self):
        # DE LÖÖPS and the queue must be started in the same method so asyncio can align their chakras or something

        self.unprocessed_transcription_queue = asyncio.Queue()

        '''
        For responsiveness, both of the things that update my chest, the audio RX, and the Web poppycock
        must be able to run simultaneously. The audio receiving loop must always be able to do its thing.
        It's easier to run these tasks separately, since they have totally different timing requirements.
        '''
        asyncio.create_task(self.transcription_interpreter_loop())
        asyncio.create_task(self.expiration_timing_loop())

    # I'm not using this method
    def brother_the_loops_must_end(self):
        # I should probably wait for them to shut down or something
        self.done = True

    # The business end! Pull a Deepgram response from the queue and turn it into chest letters.
    async def handle_transcript(self, transcript, response_words, request_id, is_final):
        # Lock out the expiration timer loop - we don't want to delete lines as we're editing them
        async with self.transcript_lock:
            # Track changes to the request ID to maintain global timestamp offsets.
            # This should only happen when an interim transcript becomes final, but Murphy's a bitch.
            if self.current_request_id != request_id:
                print(f'Now receiving results from request {request_id}')
                self.current_request_id = request_id
                if self.timebase is None:
                    # Record T0 - the wall-clock time when the first of this whole procession of transcripts began.
                    self.timebase = datetime.datetime.now()
                    self.request_time_offsets[request_id] = 0
                else:
                    # Time offsets are in seconds since T0
                    self.request_time_offsets[request_id] = (datetime.datetime.now() - self.timebase).total_seconds()

            words = [TranscriptionWord(i['word'],  # These should never brick - we already validated them
                                       i['start'],
                                       i['end'],
                                       time_offset=self.request_time_offsets[request_id],
                                       request_id=request_id)
                     for i in response_words]

            '''
            Final transcripts don't change. By tracking them separately, we don't need to compare every incoming word
            to every received word and constantly update every transcript. We just clobber the interim stuff with
            the new and ignore the final stuff. 
            There should only be a single interim request going at a time, but Murphy's got my number.
            '''
            if is_final:
                print('This transcript has been finalized.')
                # Append this to the ever-growing katamari of finalized transcripts.
                # The linebreak makes more visual sense than smooshing distinct thoughts into one big paragraph.
                self.finalized_transcript = self.finalized_transcript + '\r' + transcript
                self.finalized_words.extend(words)
                # When no interim transcript in the works, those data are obsolete.
                del self.interim_words[:]
                self.interim_transcript = ''
                self.current_request_id = -1
            else:
                # Just overwrite the old interim stuff with the new.
                # Changes come into play when we RENDER - remember that we still have currently_displayed_words
                self.interim_transcript = transcript
                self.interim_words = words

            '''
            Writing support for multiple concurrent interim transcripts would be a nightmare, but Deepgram's docs 
            assure me that they only maintain one interim transcript at a time.
            But I have trust issues, and wacky TCP shenanigans are also in play. 
            Instead of solving a bug that I'm not sure exists, I want my code to bring its presence to my attention. 
            By crashing.
            Premature optimization is the root of all evil! Don't solve problems until they exist!
            '''
            for word in self.interim_words:
                assert word.request_id == self.current_request_id

            # All that crap for this.
            await self.render()

    # At our leisure, we pull Deepgram API responses from their queue and chooch 'em up.
    # This should always INCREASE the amount of stuff to display, never remove stuff.
    async def transcription_interpreter_loop(self):
        while not self.done:
            response = await self.unprocessed_transcription_queue.get()

            try:
                # I should probably move this back to the bit of the code that inserts it into the queue.
                # Gotta consolidate all the Deepgram SDK funsies in case the spec changes.
                transcript = response['channel']['alternatives'][0]['transcript']
                words = response['channel']['alternatives'][0]['words']
                request_id = response['metadata']['request_id']
                is_final = response['is_final']

                await self.handle_transcript(transcript, words, request_id, is_final)
            except KeyError:
                # Status messages, metadata, confirmation, and who knows what else
                print('Not a transcription')

        print('Transcription interpreter loop is dead')

    '''
    It doesn't matter how fast I talk - the subtitles are meaningless unless the viewer has time to read 'em. 
    When every word on the top line has been displayed for a sec or so, we want to scroll the whole 
    thing up to show more transcript. Remember that I'm an obnoxious bastard AND the display is small - I can 
    overflow the whole screen with just PART of a sentence. 
    Even if I'm still talkin, we need to keep those captions rolling. 
    '''
    async def expiration_timing_loop(self):
        while not self.done:
            # If there's nothing on muh chest, this loop should idle as non-blockingly as possible.
            if len(self.currently_displayed_words):
                # line_expiration_start_time doesn't necessarily represent the last time we cleared a line.
                # If the top line changes or grows, we need to adjust it to give the viewer time to catch up.
                seconds_since_last_clear = (datetime.datetime.now() - self.line_expiration_start_time).total_seconds()
                if seconds_since_last_clear >= self.minimum_line_display_time:
                    # We're maintaining our data - keep other loop from adding more til we're done.
                    async with self.transcript_lock:
                        print('Top line expired')
                        self.line_expiration_start_time = datetime.datetime.now()

                        # Transfer cleared words to a buffer so we know not to display them again
                        self.word_discard_pile.extend(self.currently_displayed_words[0])

                        # Show the carnage
                        await self.render()

                        if not len(self.currently_displayed_words):
                            # This is the worst place to do this, except for the alternatives
                            print('CLEARED THE BOARD!!! WOOOO')
                            self.timebase = None
                            self.request_time_offsets.clear()
                            self.line_expiration_start_time = datetime.datetime.min
                            del self.word_discard_pile[:]
                            self.finalized_transcript = ''
                            del self.finalized_words[:]
                            self.interim_transcript = ''
                            del self.interim_words[:]
                            self.current_request_id = -1
                            del self.currently_displayed_words[:]
                            print('Wiped everything')

            # Asyncio's sleep releases control instead of blocking
            await asyncio.sleep(0.1)

        print('Expiration timing loop is dead')

    # Take whatever we got and display it on my fat, hairy pecs.
    async def render(self):
        print('Rendering start')

        # For displaying, there's no difference between the final and interim transcripts.
        # Combine 'em as if the interim was finalized.
        combined_words = self.finalized_words + self.interim_words

        if len(self.finalized_transcript):
            # Carriage return between distinct statements makes more visual sense
            if len(self.interim_transcript):
                combined_transcript = self.finalized_transcript + '\r' + self.interim_transcript
            else:
                combined_transcript = self.finalized_transcript
        else:
            combined_transcript = self.interim_transcript

        # Remove whitespace and occasional double spaces
        combined_transcript = combined_transcript.strip().replace('  ', ' ')

        if len(combined_transcript):  # Remember that we also call render() to clear the screen when we're all done.
            # Deepgram transcripts aren't always capitalized... but they shoooooould...
            combined_transcript = combined_transcript[0].upper() + combined_transcript[1:]

            # The TranscriptionWords got the timestamps, but we're displaying the formatted transcript.
            # This method figures out which WORDS have expired, then slices 'em off the TRANSCRIPT.
            for spent_word in self.word_discard_pile:
                # This assumes words stay in order. I sure hope they do...
                if spent_word.coincides_with(combined_words[0]) or combined_words[0].coincides_with(spent_word):
                    # We've already given plenty of time to read this word by now. Do not render it.
                    delete_up_to_here = combined_transcript.casefold().index(combined_words[0].text) \
                                        + len(combined_words[0].text)  # Delete the end of the word too!
                    # This can leave some cruft behind, so git it.
                    combined_transcript = combined_transcript[delete_up_to_here:].lstrip(punctuation).lstrip()
                    # Seems weird to maintain a temporary list, but we need this to reconstruct
                    # the currently_displayed_words from the output of the blit() method.
                    del combined_words[0]

            # Capitalize letters that start statements and appear after punctuation
            # I ripped this off StackOverflow. I can't regex my way out of a paragraph describing a paper bag.
            combined_transcript = re.sub("([.?!\r])\s*([a-zA-Z])", lambda p: p.group(0).upper(), combined_transcript)
        else:
            combined_words = []

        # We're about to overwrite these when we update the display, so we need to copy them by value.
        previously_displayed_words = copy.deepcopy(self.currently_displayed_words)

        self.display.fill((0, 0, 0))  # Black
        display_state = self.blit_as_much_wrapped_text_as_possible(combined_transcript)

        if len(display_state):
            print(f"Display is now:")
            for line in display_state:
                print(f'  {line}')
        else:
            print('Display is now BLANK!')

        pygame.display.update()

        # Match display state output strings to TranscriptionWords to track what's being displayed onscreen
        del self.currently_displayed_words[:]
        pointerino = 0
        for i, line in enumerate(display_state):
            if len(line):
                self.currently_displayed_words.append([])
            for token in line.split():
                if combined_words[pointerino].represents(token):
                    self.currently_displayed_words[i].append(combined_words[pointerino])
                    pointerino += 1
                else:
                    # The transcript fell out of sync with the words! Shouldn't happen, but...
                    raise RuntimeError(f'Zack is a bad programmer - word {combined_words[pointerino].text} '
                                       f'does not represent transcript token {token}.')

        # If the top line changed, we need to reset the expiration timer so folks catch up with my very important words
        if len(self.currently_displayed_words) and len(self.currently_displayed_words[0]):
            # If the top line now represents a longer stretch of time, we added a word!
            if not len(previously_displayed_words) or \
                    not len(previously_displayed_words[0]) or \
                    previously_displayed_words[0][-1].ended_before(
                        self.currently_displayed_words[0][-1].get_offset_end()):
                print('Put a new word on the top line! Reset that timer!')
                self.line_expiration_start_time = datetime.datetime.now()

            # If the line was full and is still full, it still might have changed
            elif len(self.currently_displayed_words) and len(previously_displayed_words) and \
                    len(self.currently_displayed_words[0]) == len(previously_displayed_words[0]):
                for x, y in zip(self.currently_displayed_words[0], previously_displayed_words[0]):
                    if x.text != y.text:
                        print(f'Top line changed - {y} is now {x}. Reset that timer!')
                        self.line_expiration_start_time = datetime.datetime.now()

        # Jeez finally
        print('Rendering done')

    # Draw some text straight to the display, automatically wrapping words and doing the carriage return rhumba.
    # Returns current state of screen as a list of strings. Each str is one displayed line of text.
    # Adapted from https://www.pygame.org/wiki/TextWrap
    def blit_as_much_wrapped_text_as_possible(self, text_to_render, aa=False, bkg=None):
        output = []
        text = copy.deepcopy(text_to_render)

        color = (0xff, 0xff, 0xff)
        textbox = pygame.Surface((1280, 480))
        rect = pygame.Rect(0, 0, 1280, 480)
        y = rect.top
        line_spacing = -2

        while text:
            i = 1

            # determine if the row of text will be outside our area
            if y + self.font_height > rect.bottom:
                break

            # determine maximum width of line
            while self.font.size(text[:i])[0] < rect.width and i < len(text) and text[i] != '\r':
                i += 1

            # if we've wrapped the text, then adjust the wrap to the last word
            if i < len(text):
                # Pygame totally ignores control chars in font rendering
                if text[i] == '\r':
                    i += 1
                else:
                    i = text.rfind(" ", 0, i) + 1

            # render the line and blit it to the surface
            if bkg:
                image = self.font.render(text[:i].replace('\r', ''), 1, color, bkg)
                image.set_colorkey(bkg)
            else:
                image = self.font.render(text[:i].replace('\r', ''), aa, color)

            textbox.blit(image, (rect.left, y))
            output.append(text[:i])
            y += self.font_height + line_spacing

            # remove the text we just blitted
            text = text[i:]

        textbox = pygame.transform.rotate(textbox, 90)
        self.display.blit(textbox, (0, 0))
        return output


# THIS ONE'S GONNA MAKE ME A STAH
class SubtitleHoodie:
    subtitle_display: SubtitleDisplay

    def __init__(self):
        # Your Deepgram API Key
        # TODO: DO NOT INCLUDE THIS IN THE REPO YOU DUMBSHIT
        self.DEEPGRAM_API_KEY = '87b9489cce2a5ee15b8e248ab2181ce9173b49b3'
        self.FRAMES_PER_BUFFER = 8192  # We need to read audio samples seriously fast, or its tiny buffer overflows
        self.SAMPLE_RATE = 44100  # I want more samples for faster peak detection

        # TODO: Noise floor is too crude to detect start-of-message, pick something sensitive
        self.NOISE_FLOOR = 125  # Quietest block that could be speech - don't send anything quieter to Deepgram
        self.QUIET_DEADLINE = 2  # If I'm quiet for this many seconds (using noise floor), stop forwarding audio

        # TODO: Anything with this
        self.done = False

        # Download retro flavor at https://www.dafont.com/vcr-osd-mono.font
        # The font's licensing is unclear, so I'm not including it here.
        project_root = os.path.dirname(os.path.abspath(__file__))
        subtitle_font = pygame.font.Font(os.path.join(project_root, 'res/VCR_OSD_MONO_1.001.ttf'), 125)
        lcd = pygame.display.set_mode((480, 1280), pygame.FULLSCREEN)
        self.subtitle_display = SubtitleDisplay(lcd, subtitle_font)

    # Simple callback that dumps the response straight in the transcription queue.
    # A loop will collect and handle it automatically.
    def handle_response(self, response):
        self.subtitle_display.unprocessed_transcription_queue.put_nowait(response)

    # Read audio. This absolutely must run as often as possible.
    async def get_a_chunk(self, stream, queue):
        # Things tend to freeze if we try to read audio before there's a full buffer's worth.
        while stream.get_read_available() < self.FRAMES_PER_BUFFER:
            await asyncio.sleep(0.005)

        # No time to interpret the audio. Dump it into a queue to deal with later, so we can immediately listen for more
        queue.put_nowait((datetime.datetime.now(), stream.read(self.FRAMES_PER_BUFFER)))

    # Keeping that buffer dry is so important it gets its own dedicated loop
    async def audio_receiver(self, stream, queue):
        stream.start_stream()
        print('Stream opened')

        while not self.done:
            await self.get_a_chunk(stream, queue)

    # God method so overburdened and all-encompassing it can cause functional programmers to projectile-diarrhea
    async def do_the_thing(self):
        last_loud_enough_timestamp = None

        def has_been_quiet_for_too_long(last_noisy_chunk_timestamp):
            return last_noisy_chunk_timestamp is None or (
                    datetime.datetime.now() - last_noisy_chunk_timestamp).total_seconds() >= self.QUIET_DEADLINE

        p = pyaudio.PyAudio()

        # Find our IQaudio Codec Zero sound board. It automatically switches between onboard and plug-in mics.
        iqaudio_product_index = -1
        print('Audio interfaces:')
        for i in range(p.get_device_count()):
            interface_name = p.get_device_info_by_index(i)["name"]
            print(f'  {i}: {interface_name}')
            if 'iqaudiocodec' in interface_name.casefold():
                iqaudio_product_index = i

        if iqaudio_product_index == -1:
            raise RuntimeError('IQAudio product (Codec Zero) not found!')

        print(f'Mic input index: {iqaudio_product_index}')

        s = p.open(input_device_index=iqaudio_product_index,
                   format=pyaudio.paInt16, rate=self.SAMPLE_RATE, channels=1,
                   input=True, output=False,
                   frames_per_buffer=self.FRAMES_PER_BUFFER, )

        q: asyncio.Queue[tuple[datetime.datetime, bytearray]] = asyncio.Queue()

        asyncio.create_task(self.audio_receiver(s, q))

        # IT'S HAPPENING
        self.subtitle_display.start_the_loops_brother()

        # OH MY GOD IT'S HAPPENING
        deepgram = Deepgram(self.DEEPGRAM_API_KEY)

        deepgram_live: LiveTranscription = await self.create_live_transcription_websocket(deepgram)
        print('Created initial live websocket')

        # If we kill the socket without sending anything, weird things happen.
        # But, we need to go in with a socket or we'll need to check 'is None' in a million places.
        # So here's the worst kind of workaround - one that costs money.
        print('sending bogus data')
        deepgram_live.send(bytearray([0] * self.FRAMES_PER_BUFFER))
        print('sent')

        # I'M GONNA... I'M GONNA... SUUUUUUBTITLE
        while not self.done:
            timestamp, incoming = await asyncio.wait_for(q.get(), None)

            rms = audioop.rms(incoming, 2)  # The 2 is the number of bytes in one sample of our 16-bit int wav

            if rms >= self.NOISE_FLOOR:
                last_loud_enough_timestamp = timestamp
                if deepgram_live.done:  # We can't reuse an instance - we need to create another one
                    print('We got some action! Creating a fresh live transcription websocket')
                    deepgram_live = await self.create_live_transcription_websocket(deepgram)
                    print('Created')

            # Kill the connection after a few seconds of silence to conserve API credit
            if has_been_quiet_for_too_long(last_loud_enough_timestamp):
                if not deepgram_live.done:
                    print('Quiet for too long. Killing live transcription websocket')
                    await deepgram_live.finish()
                    print('Done')
            else:
                deepgram_live.send(incoming)

        s.stop_stream()  # Cleanly exit
        s.close()

        # Indicate that we've finished sending data by sending a zero-byte message to the Deepgram streaming endpoint,
        # and wait until we get back the final summary metadata object
        await deepgram_live.finish()

    # We can't reuse an instance of the Deepgram SDK websocket thingy - if we hang up, we need to generate a fresh one.
    # Note that this has absolutely no relationship to Request IDs - those are split by pauses between statements.
    async def create_live_transcription_websocket(self, deepgram):
        # Create a websocket connection to Deepgram
        # In this example, punctuation is turned on, interim results are turned off, and language is set to UK English.
        try:
            deepgram_live = await deepgram.transcription.live(
                {
                    'language': 'en-US',  # Change to en-UK or something if you're one of THOSE people
                    'encoding': 'linear16', 'sample_rate': self.SAMPLE_RATE,  # Corresponds to PyAudio config
                    'punctuate': True,          # I'm such a chad my speech has punctuation
                    'interim_results': False,    # Return ANYTHING as soon as possible, for responsiveness
                    'diarize': True,            # TODO: Distinguish yours truly from whoever I'm talking to
                })
        except Exception as e:
            print(f'Could not open socket: {e}')
            raise

        # Listen for the connection to close
        deepgram_live.registerHandler(deepgram_live.event.CLOSE, lambda c: print(f'Connection closed with code {c}.'))
        # Listen for any transcripts received from Deepgram and write them to the console
        deepgram_live.registerHandler(deepgram_live.event.TRANSCRIPT_RECEIVED, lambda c: self.handle_response(c))
        return deepgram_live


# Run the whole module as a script, see if I care
if __name__ == '__main__':
    # I have no idea where I'm supposed to do this.
    pygame.init()
    pygame.mouse.set_visible(False)

    # Without this, crashes will also be asynchronous, making debugging sheer h*ck
    def oopsie(ser_loopinus_of_async_upon_io, context):
        exception = context.get('exception')
        raise exception

    loop = asyncio.get_event_loop()
    loop.set_exception_handler(oopsie)

    the_project_thats_gonna_make_me_bigger_than_dunkey = SubtitleHoodie()
    asyncio.run(the_project_thats_gonna_make_me_bigger_than_dunkey.do_the_thing())
