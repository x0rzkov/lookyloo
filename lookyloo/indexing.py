#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from urllib.parse import urlsplit
from typing import List, Tuple, Set, Dict, Optional

from redis import Redis
from har2tree import CrawledTree

from .helpers import get_socket_path, get_public_suffix_list


class Indexing():

    def __init__(self) -> None:
        self.redis: Redis = Redis(unix_socket_path=get_socket_path('indexing'), decode_responses=True)

    def clear_indexes(self):
        self.redis.flushdb()

    # ###### Cookies ######

    @property
    def cookies_names(self) -> List[Tuple[str, float]]:
        return self.redis.zrevrange('cookies_names', 0, -1, withscores=True)

    def cookies_names_number_domains(self, cookie_name: str) -> int:
        return self.redis.zcard(f'cn|{cookie_name}')

    def cookies_names_domains_values(self, cookie_name: str, domain: str) -> List[Tuple[str, float]]:
        return self.redis.zrevrange(f'cn|{cookie_name}|{domain}', 0, -1, withscores=True)

    def get_cookie_domains(self, cookie_name: str) -> List[Tuple[str, float]]:
        return self.redis.zrevrange(f'cn|{cookie_name}', 0, -1, withscores=True)

    def get_cookies_names_captures(self, cookie_name: str) -> List[Tuple[str, str]]:
        return [uuids.split('|') for uuids in self.redis.smembers(f'cn|{cookie_name}|captures')]  # type: ignore

    def index_cookies_capture(self, crawled_tree: CrawledTree) -> None:
        if self.redis.sismember('indexed_cookies', crawled_tree.uuid):
            # Do not reindex
            return
        self.redis.sadd('indexed_cookies', crawled_tree.uuid)

        pipeline = self.redis.pipeline()
        already_loaded: Set[Tuple[str, str]] = set()
        for urlnode in crawled_tree.root_hartree.url_tree.traverse():
            if hasattr(urlnode, 'cookies_received'):
                for domain, cookie, _ in urlnode.cookies_received:
                    name, value = cookie.split('=', 1)
                    if (name, domain) in already_loaded:
                        # Only add cookie name once / capture
                        continue
                    already_loaded.add((name, domain))
                    pipeline.zincrby('cookies_names', 1, name)
                    pipeline.zincrby(f'cn|{name}', 1, domain)
                    pipeline.sadd(f'cn|{name}|captures', f'{crawled_tree.uuid}|{urlnode.uuid}')
                    pipeline.zincrby(f'cn|{name}|{domain}', 1, value)

                    pipeline.sadd('lookyloo_domains', domain)
                    pipeline.sadd(domain, name)
        pipeline.execute()

    def aggregate_domain_cookies(self):
        psl = get_public_suffix_list()
        pipeline = self.redis.pipeline()
        for cn, cn_freq in self.cookies_names:
            for domain, d_freq in self.get_cookie_domains(cn):
                tld = psl.get_tld(domain)
                main_domain_part = domain.strip(f'.{tld}').split('.')[-1]
                pipeline.zincrby('aggregate_domains_cn', cn_freq, f'{main_domain_part}|{cn}')
                pipeline.zincrby('aggregate_cn_domains', d_freq, f'{cn}|{main_domain_part}')
        pipeline.execute()
        aggregate_domains_cn = self.redis.zrevrange('aggregate_domains_cn', 0, -1, withscores=True)
        aggregate_cn_domains = self.redis.zrevrange('aggregate_cn_domains', 0, -1, withscores=True)
        self.redis.delete('aggregate_domains_cn')
        self.redis.delete('aggregate_cn_domains')
        return {'domains': aggregate_domains_cn, 'cookies': aggregate_cn_domains}

    # ###### Body hashes ######

    @property
    def ressources(self) -> List[Tuple[str, float]]:
        return self.redis.zrevrange('body_hashes', 0, 200, withscores=True)

    def ressources_number_domains(self, h: str) -> int:
        return self.redis.zcard(f'bh|{h}')

    def body_hash_fequency(self, body_hash: str) -> Dict[str, float]:
        return {'hash_freq': int(self.redis.zscore('body_hashes', body_hash)),
                'hash_domains_freq': int(self.redis.zcard(f'bh|{body_hash}'))}

    def index_body_hashes_capture(self, crawled_tree: CrawledTree) -> None:
        if self.redis.sismember('indexed_body_hashes', crawled_tree.uuid):
            # Do not reindex
            return
        self.redis.sadd('indexed_body_hashes', crawled_tree.uuid)

        pipeline = self.redis.pipeline()
        for urlnode in crawled_tree.root_hartree.url_tree.traverse():
            for h in urlnode.resources_hashes:
                pipeline.zincrby('body_hashes', 1, h)
                pipeline.zincrby(f'bh|{h}', 1, urlnode.hostname)
                # set of all captures with this hash
                pipeline.sadd(f'bh|{h}|captures', crawled_tree.uuid)
                # ZSet of all urlnode_UUIDs|full_url
                pipeline.zincrby(f'bh|{h}|captures|{crawled_tree.uuid}', 1, f'{urlnode.uuid}|{urlnode.hostnode_uuid}|{urlnode.name}')

        pipeline.execute()

    def get_hash_uuids(self, body_hash: str) -> Tuple[str, str, str]:
        capture_uuid = self.redis.srandmember(f'bh|{body_hash}|captures')
        entry = self.redis.zrange(f'bh|{body_hash}|captures|{capture_uuid}', 0, 1)[0]
        urlnode_uuid, hostnode_uuid, url = entry.split('|', 2)
        return capture_uuid, urlnode_uuid, hostnode_uuid

    def get_body_hash_captures(self, body_hash: str, filter_url: Optional[str]=None,
                               filter_capture_uuid: Optional[str]=None,
                               limit: int=20) -> Tuple[int, List[Tuple[str, str, str, bool]]]:
        to_return: List[Tuple[str, str, str, bool]] = []
        all_captures: Set[str] = self.redis.smembers(f'bh|{body_hash}|captures')  # type: ignore
        len_captures = len(all_captures)
        for capture_uuid in list(all_captures)[:limit]:
            if capture_uuid == filter_capture_uuid:
                # Used to skip hits in current capture
                len_captures -= 1
                continue
            for entry in self.redis.zrevrange(f'bh|{body_hash}|captures|{capture_uuid}', 0, -1):
                url_uuid, hostnode_uuid, url = entry.split('|', 2)
                hostname: str = urlsplit(url).hostname
                if filter_url:
                    to_return.append((capture_uuid, hostnode_uuid, hostname, url == filter_url))
                else:
                    to_return.append((capture_uuid, hostnode_uuid, hostname, False))
        return len_captures, to_return

    def get_body_hash_domains(self, body_hash: str) -> List[Tuple[str, float]]:
        return self.redis.zrevrange(f'bh|{body_hash}', 0, -1, withscores=True)
