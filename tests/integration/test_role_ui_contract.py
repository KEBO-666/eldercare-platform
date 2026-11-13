"""Role themes must preserve authorization and accessible navigation contracts."""
import re
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from app.core.deps import permission_codes
from app.core.security import create_access_token
from app.models import Alert, Bill, BillDetail, Permission, Role

THEMES = [('elder', 'warm'), ('nurse', 'care'), ('grid', 'blue'),
          ('org_admin', 'blue'), ('street', 'night'), ('super', 'night')]


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def matching(self, tag, **attrs):
        return [values for name, values in self.tags if name == tag
                and all(values.get(key.replace('_', '-')) == value for key, value in attrs.items())]

    def links(self):
        return {urlsplit(attrs['href']).path for tag, attrs in self.tags if tag == 'a' and attrs.get('href')}


def page_as(client, user, path):
    client.cookies.clear()
    client.cookies.set('access_token', create_access_token(user.id, {'role': user.role.code, 'username': user.username}))
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200, response.text
    return response


def assert_theme(response, expected):
    page = Page(response.text)
    themes = [attrs['data-theme'] for _, attrs in page.tags if 'data-theme' in attrs]
    assert themes and set(themes) == {expected}
    assert page.matching('link', href='/static/css/role-themes.css')
    return page


def assert_navigation(page):
    assert page.matching('script', src='/static/js/navigation.js')
    assert page.matching('aside', id='sidebar')
    assert page.matching('a', href='/', aria_current='page', data_nav='home')
    toggles = page.matching('button', aria_controls='sidebar', aria_expanded='false')
    assert toggles and all(button.get('aria-label') for button in toggles)
    close_buttons = [attrs for tag, attrs in page.tags if tag == 'button' and (
        'sidebar-close' in attrs.get('class', '').split() or 'data-sidebar-close' in attrs
    )]
    assert close_buttons and all(button.get('aria-label') for button in close_buttons)


def assert_home_links_authorized(response, user):
    main = response.text.split('<main', 1)[1].split('</main>', 1)[0]
    links = Page(main).links()
    permissions = permission_codes(user)
    requirements = {
        '/appointments/new': 'appointment:create', '/appointments': 'appointment:read',
        '/orders': 'order:read', '/elders': 'elder:read', '/dispatch': 'order:dispatch',
        '/monitoring': 'health:read', '/alerts': 'alert:read', '/billing': 'bill:read',
        '/stats': 'stats:read', '/admin/audit': 'admin:audit',
    }
    for path, permission in requirements.items():
        if permission not in permissions and '*' not in permissions:
            assert path not in links, f'{user.username} exposes unauthorized homepage action {path}'
    # These shortcuts must follow capabilities rather than a hardcoded role list.
    for path in ('/appointments/new', '/dispatch', '/orders', '/stats', '/admin/audit'):
        permission = requirements[path]
        assert (path in links) is (permission in permissions or '*' in permissions), path
    return links


@pytest.mark.parametrize('role,theme', THEMES)
def test_six_role_theme_navigation_and_home_actions_preserve_permissions(client, accounts, role, theme):
    user = accounts['users'][role]
    for query in ('', '?theme=night&ui_theme=warm'):
        response = page_as(client, user, '/' + query)
        page = assert_theme(response, theme)
        assert_navigation(page)
        assert_home_links_authorized(response, user)
        if role == 'nurse':
            assert '/billing' not in page.links()
            assert '上月账单金额' not in response.text and '个人自付' not in response.text
        if role == 'elder':
            assert not any(path.startswith('/monitoring') for path in page.links())
            assert '我的健康' not in response.text
    if role == 'elder':
        assert client.get('/monitoring').status_code == 403
    if role == 'nurse':
        assert client.get('/billing').status_code == 403


def test_anonymous_redirect_and_login_theme_ignore_theme_query(client):
    for path in ('/', '/orders', '/monitoring', '/alerts'):
        response = client.get(path + '?theme=night', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'].startswith('/login?next=')
        assert 'data-nav=' not in response.text
    for path in ('/login', '/login?theme=night&ui_theme=warm'):
        response = client.get(path)
        assert response.status_code == 200
        assert_theme(response, 'blue')
    for path in ('/static/css/role-themes.css', '/static/js/navigation.js'):
        asset = client.get(path)
        assert asset.status_code == 200 and asset.content.strip()


def test_health_only_user_has_no_alert_leak_or_unauthorized_home_shortcuts(client, db, accounts):
    user = accounts['users']['org_admin']
    user.role.permissions = [db.scalar(select(Permission).where(Permission.code == 'health:read'))]
    db.add(Alert(elder_id=accounts['elder'].id, level='red', status='pending', summary='ROLE-UI-PRIVATE-ALERT'))
    db.commit()
    for path in ('/', f"/monitoring/{accounts['elder'].id}"):
        response = page_as(client, user, path + '?theme=night')
        page = assert_theme(response, 'blue')
        assert 'ROLE-UI-PRIVATE-ALERT' not in response.text
        assert not any(link.startswith('/alerts') for link in page.links())
        assert not page.matching('a', data_nav='alerts')
        if path == '/':
            assert_home_links_authorized(response, user)
    assert client.get('/alerts').status_code == 403


def test_unknown_custom_role_uses_blue_without_inheriting_privileged_actions(client, db, accounts):
    user = accounts['users']['grid']
    custom = Role(code='custom_ui_role', name='自定义只读角色', data_scope='grid')
    custom.permissions = [db.scalar(select(Permission).where(Permission.code == 'order:read'))]
    db.add(custom)
    db.flush()
    user.role = custom
    db.commit()
    response = page_as(client, user, '/?theme=night&role=super')
    page = assert_theme(response, 'blue')
    assert_home_links_authorized(response, user)
    assert '/admin/audit' not in page.links() and '/dispatch' not in page.links()
    assert '自定义只读角色' in response.text


def test_home_bill_totals_match_visible_billing_details_without_leaking_other_elders(
    client, db, accounts, orgs, make_elder,
):
    peer = make_elder()
    outsider = make_elder(community_org_id=orgs['community2'].id, grid_org_id=orgs['grid3'].id)
    shared = Bill(code='UI-SHARED-OCT', period='2026-10', community_org_id=orgs['community1'].id,
                  total_amount=Decimal('488.46'), self_pay_amount=Decimal('90.46'), status='confirmed')
    outside = Bill(code='UI-OUTSIDE-OCT', period='2026-10', community_org_id=orgs['community2'].id,
                   total_amount=Decimal('9999.43'), self_pay_amount=Decimal('8888.43'), status='confirmed')
    db.add_all([shared, outside])
    db.flush()
    db.add_all([
        BillDetail(bill_id=shared.id, elder_id=accounts['elder'].id,
                   amount=Decimal('131.17'), self_pay_amount=Decimal('23.17')),
        BillDetail(bill_id=shared.id, elder_id=peer.id,
                   amount=Decimal('357.29'), self_pay_amount=Decimal('67.29')),
        BillDetail(bill_id=outside.id, elder_id=outsider.id,
                   amount=Decimal('9999.43'), self_pay_amount=Decimal('8888.43')),
    ])
    db.commit()
    with freeze_time(datetime(2026, 11, 13, 9)):
        for role, expected_total, expected_self_pay in [('elder', '131.17', '23.17'), ('grid', '488.46', '90.46')]:
            home = page_as(client, accounts['users'][role], '/')
            card = re.search(r'<a\b[^>]*class="stat-card[^"\n]*"[^>]*href="/billing"[^>]*>(.*?)</a>',
                             home.text, re.DOTALL)
            assert card, 'Authorized billing card is missing'
            assert re.search(r'class="stat-value">\s*' + re.escape(expected_total) + r'\s*</div>', card[1])
            assert expected_self_pay in card[1]
            billing = page_as(client, accounts['users'][role], '/billing?period=2026-10')
            assert re.findall(r'class="stat-value">\s*([^<]+)', billing.text)[0].strip() == expected_total
            for text in (home.text, billing.text):
                assert '9,999.43' not in text and '8,888.43' not in text
            if role == 'elder':
                assert '488.46' not in home.text and '357.29' not in home.text
        nurse = page_as(client, accounts['users']['nurse'], '/')
        assert '/billing' not in Page(nurse.text).links()
        for private_amount in ('131.17', '488.46', '9,999.43', '8,888.43'):
            assert private_amount not in nurse.text
