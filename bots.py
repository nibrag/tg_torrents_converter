import sys
import asyncio
import urllib.parse
import binascii
import hashlib
import logging
import libtorrent
import better_bencode
import time
from concurrent.futures import ThreadPoolExecutor
from aiotg import Bot

formatter = logging.Formatter('[%(levelname)s] %(message)s (%(asctime)s)')
file_handler = logging.FileHandler('torrent-helper.log')
stdout_handler = logging.StreamHandler(sys.stdout)
file_handler.setFormatter(formatter)
stdout_handler.setFormatter(formatter)

logger = logging.getLogger('torrent-helper')
logger.addHandler(file_handler)
logger.addHandler(stdout_handler)
logger.setLevel(logging.INFO)

t2m_bot = Bot(api_token='')
m2t_bot = Bot(api_token='')

proc_pool = ThreadPoolExecutor(15)


@t2m_bot.command(r'/(start|help)')
async def start(chat, match):
    await chat.send_text('Send torrent file here')


@m2t_bot.command(r'/(start|help)')
async def start(chat, match):
    await chat.send_text('Send magnet link here')


@t2m_bot.handle('document')
async def torrent2magnet(chat, document):
    logger.info('torrent2magnet: session start [%d]', chat.sender['id'])

    try:
        torrent = await t2m_bot.get_file(document['file_id'])

        async with t2m_bot.download_file(torrent['file_path']) as resp:
            if resp.status == 200:
                try:
                    metadata = better_bencode.loads(await resp.read())
                    if b'info' not in metadata:
                        raise Exception('No `info` field in meta data')

                    hashcontents = better_bencode.dumps(metadata[b'info'])
                    digest = hashlib.sha1(hashcontents).digest()
                    info_hash = binascii.hexlify(digest).decode('ascii')
                except Exception:
                    logger.exception('torrent2magnet: Wrong meta data')
                    await chat.send_text('Failed convert torrent file to magnet link '
                                         '(wrong metadata). Sorry 😫')
                else:
                    extra_params = {}

                    if b'name' in metadata[b'info']:
                        extra_params.update({'dn': metadata[b'info'][b'name'].decode('ascii')})
                    if b'length' in metadata[b'info']:
                        extra_params.update({'xl': metadata[b'info'][b'length']})
                    if b'announce' in metadata:
                        extra_params.update({'tr': metadata[b'announce'].decode('ascii')})

                    ep = ''
                    if extra_params:
                        ep = '&' + urllib.parse.urlencode(extra_params)
                    await chat.send_text('magnet:?xt=urn:btih:%s%s' % (info_hash, ep))
            else:
                raise RuntimeError('HTTP error [status={}]'.format(resp.status))
    except Exception:
        await chat.send_text('Failed convert torrent file to magnet. Sorry 😫')
        logger.exception('torrent2magnet: unhandled error')


@m2t_bot.command('')
async def magnet2torrent(chat, match):
    logger.info('magnet2torrent: session start [%d]', chat.sender['id'])

    loop = asyncio.get_event_loop()
    magnet_link = chat.message['text']

    chunks = urllib.parse.urlparse(magnet_link)
    if chunks.scheme != 'magnet':
        await chat.send_text('Invalid magnet link 😫')
        return

    await chat.send_text('Fetching meta data. Wait please... 🙏 It may take up to 3 minutes.')
    torrent = await loop.run_in_executor(proc_pool, magnet2torrent_worker, magnet_link)
    if torrent:
        file_name, torrent_content = torrent
        await chat.send_document(document=torrent_content, caption=file_name)
    else:
        await chat.send_text('Failed convert magnet link to torrent file. Sorry 😫')


def magnet2torrent_worker(magnet):
    logger.info('magnet2torrent: start [%s]', magnet)

    session = libtorrent.session()
    params = libtorrent.parse_magnet_uri(magnet)

    # bug: TypeError: No registered converter was able to produce a C++
    # rvalue of type bytes from this Python object of type sha1_hash
    params.update({'info_hash': params['info_hash'].to_bytes()})
    handle = session.add_torrent(params)
    if not handle.is_valid():
        logger.error('magnet2torrent: invalid handle')

    time_lim = time.time() + 3*60

    while not handle.has_metadata():
        time.sleep(0.1)
        if time.time() > time_lim:
            logger.info('magnet2torrent: the waiting time of metadata has expired')
            break

    session.pause()
    try:
        torinfo = handle.get_torrent_info()
        if not torinfo:
            raise ValueError('magnet2torrent: failed getting torrent info')
        torfile = libtorrent.create_torrent(torinfo)
    except Exception:
        logger.exception('magnet2torrent: failed creating torrent file')
        return

    try:
        torrent_content = libtorrent.bencode(torfile.generate())
        if torrent_content:
            logger.info('magnet2torrent: done [%s]', magnet)
            return torinfo.name() + '.torrent', torrent_content
        else:
            logger.error('magnet2torrent: empty torrent content body [%s]', magnet)
    except Exception:
        logger.exception('magnet2torrent: torrent generating problem [%s]', magnet)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        bots = [t2m_bot.loop(), m2t_bot.loop()]
        loop.run_until_complete(asyncio.wait(bots))
    except KeyboardInterrupt:
        t2m_bot.stop()
        m2t_bot.stop()
        loop.stop()
