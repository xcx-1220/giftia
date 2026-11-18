'use strict';

const CACHE = 'ella-shell-harness-v3';
const SHELL = new Set(['/', '/chat.css', '/chat.js', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png']);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await cache.addAll([...SHELL].map(url => new Request(url, { cache: 'reload', credentials: 'omit' })));
    await self.skipWaiting();
  })());
});

self.addEventListener('push', event => {
  event.waitUntil((async () => {
    const response = await fetch('/api/push/last', {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) return;
    const message = await response.json();
    if (message.text) await self.registration.showNotification(message.title || '艾拉', {
      body: message.text.slice(0, 120), icon: '/icons/icon-192.png', tag: 'aila-outreach',
    });
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const open = await clients.matchAll({type:'window', includeUncontrolled:true});
    const current = open.find(client => new URL(client.url).origin === self.location.origin);
    if (current) return current.focus();
    return clients.openWindow('/');
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const old = (await caches.keys()).filter(key => key.startsWith('ella-shell-') && key !== CACHE);
    await Promise.all(old.map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || !SHELL.has(url.pathname)) return;
  event.respondWith((async () => {
    try {
      return await fetch(request, {cache: 'reload'});
    } catch {
      return (await caches.match(url.pathname, { cacheName: CACHE })) || Response.error();
    }
  })());
});
