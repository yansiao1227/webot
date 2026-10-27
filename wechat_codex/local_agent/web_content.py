"""将微信来源元数据适配为通用网页工具输入。"""
from tool.content_reading import web


def retrieve(card, options):
    return web.read_sources(card['source_urls'], {**options,
        'browser': card.get('card_kind') == 'app-message' and bool(options.get('profile_dir')),
        'public_only': True})


def fetch(url, timeout=20):
    return web.fetch(url, timeout, public_only=True)


def browser(url, options):
    return web.browser(url, {**options, 'public_only': True})


def __getattr__(name):
    return getattr(web, name)
