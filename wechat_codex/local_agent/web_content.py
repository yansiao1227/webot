"""将微信来源元数据适配为通用网页工具输入。"""
from tool.content_reading import web
from tool.content_reading import platforms


def retrieve(card, options):
    options = {**options,
        'browser': card.get('card_kind') == 'app-message' and bool(options.get('profile_dir')),
        'public_only': True}
    results = []
    for url in card['source_urls'][:2]:
        attempt = None
        if card.get('card_kind') == 'app-message' and platforms.platform(url):
            try:
                attempt = (platforms.read(url, output_dir=options['output_dir']) if options.get('output_dir')
                           else platforms.read(url))
            except ValueError as error:
                attempt = {'state': 'unavailable', 'reason': str(error)}
            if attempt.get('state') == 'read':
                results.append(attempt)
                continue
        value = web.read_sources([url], options)[0]
        if attempt is not None:
            value['platform_attempt'] = attempt
        results.append(value)
    return results


def fetch(url, timeout=20):
    return web.fetch(url, timeout, public_only=True)


def browser(url, options):
    return web.browser(url, {**options, 'public_only': True})


def __getattr__(name):
    return getattr(web, name)
