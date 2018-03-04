import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.forms.models import model_to_dict
from backend import settings
from sanguo.constants import TOKEN_EXPIRE_AFTER, BattleState
from sanguo.models import Heroes, HeroOwnership, CityOwnership, UserBattleInfo, Cities
import json
import random
from django.core import serializers


def hero_view(requests):
    hero_list = serializers.serialize("json", Heroes.objects.all())
    return JsonResponse({"heroes": json.loads(hero_list)})


@csrf_exempt
def login_view(request):
    address = request.POST.get('address')
    if not address:
        return JsonResponse({"err_code": 401, "msg": "address为空"})
    request.session['uid'] = address
    request.session.set_expiry(TOKEN_EXPIRE_AFTER)

    # {
    #     int id;
    #     string sessionId;
    #     string name;
    #     string address;
    #     int soldier;
    #     int heroList[];
    #     int cityList[];
    # }
    player_info = {
        "session_id": request.session.session_key,
        "name": address,
        "address": address,
    }
    user_battle_info = get_user_battle_info(address)
    player_info.update(user_battle_info)
    map_info = get_map_info()
    return JsonResponse({"err_code": 0, "err_msg": "", "player_info": player_info, "map_info": map_info})


def get_current_battle_state():
    current_state = settings.user_redis.get("battle_state")
    if not current_state:
        return BattleState.get_hero
    return int(current_state)


def get_current_countdown_timestamp():
    current_countdown_timestamp = settings.user_redis.get("state_countdown")
    if not current_countdown_timestamp:
        return -1
    return int(current_countdown_timestamp)


def get_user_battle_info(address):
    """
    int soldier;
    int heroList[];
    int cityList[];
    """
    heroes = HeroOwnership.objects.filter(address=address)
    cities = CityOwnership.objects.filter(address=address)
    try:
        user_battle = UserBattleInfo.objects.get(address=address)
    except Exception as err:
        user_battle = []
    hero_list = []
    city_list = []
    for hero in heroes:
        try:
            hero_list.append(model_to_dict(Heroes.objects.get(pk=hero.hero_id)))
        except Exception as err:
            print("err: %s, traceback: %s" % (err, traceback.format_exc()))
    for city in cities:
        try:
            city_list.append(model_to_dict(Cities.objects.get(pk=city.city_id)))
        except Exception as err:
            print("err: %s, traceback: %s" % (err, traceback.format_exc()))
    return {
        "state": get_current_battle_state(),
        "countdown": get_current_countdown_timestamp(),
        "address": address,
        "heroes": hero_list,
        "cities": city_list,
        "soldier": user_battle.soldier if user_battle else 0
    }


def get_map_info():
    cities = Cities.objects.all()
    city_list = [model_to_dict(city) for city in cities]
    for city in city_list:
        city_ownership = CityOwnership.objects.filter(city_id=city['id']).first()
        if city_ownership:
            city['owner_info'] = get_user_battle_info(city_ownership.address)
        else:
            city['owner_info'] = {}
    return city_list


def my_address_view(request):
    address = request.session.get("uid", "")
    if not address:
        return JsonResponse({"err_code": 401, "msg": "用户未登录 请重新登录"})

    return JsonResponse({"err_code": 0, "address": address})


def user_info_view(request):
    address = request.GET.get("address")
    battle_info = get_user_battle_info(address)
    return JsonResponse(battle_info)


def get_my_hero_view(request):
    if get_current_battle_state() != BattleState.get_hero:
        return JsonResponse({"err_code": -1, "msg": "当前不是获取英雄阶段"})
    address = request.session.get("uid", "")
    if not address:
        return JsonResponse({"err_code": 401, "msg": "用户未登录 请重新登录"})
    heroes = HeroOwnership.objects.filter(address=address)
    if heroes:
        return JsonResponse({"err_code": -1, "msg": "你已经有英雄了 无法重复获取"})
    cities = CityOwnership.objects.filter(address=address)
    if cities:
        return JsonResponse({"err_code": -1, "msg": "你已经有城市了 无法重复获取"})
    try:
        soldier = UserBattleInfo.objects.get(address=address)
        if soldier.soldier > 0:
            return JsonResponse({"err_code": -1, "msg": "你已经拥有兵力 无法重复获取"})
    except Exception as err:
        pass
    heroes = list(Heroes.objects.all())
    heroes = random.sample(heroes, 3)
    for hero in heroes:
        hero_ownership = HeroOwnership(address=address, hero_id=hero.pk)
        hero_ownership.save()
    cities = Cities.objects.all()
    not_occupied_cities = [city for city in cities if not CityOwnership.objects.filter(city_id=city.pk).exists()]
    print("noc", not_occupied_cities)
    if not_occupied_cities:
        city = random.sample(not_occupied_cities, 1)[0]
        city_ownership = CityOwnership(address=address, city_id=city.pk)
        city_ownership.save()
    user_battle_info = UserBattleInfo(address=address, soldier=20000)
    user_battle_info.save()
    return JsonResponse({"err_code": 0, "msg": "", "countdown": get_current_countdown_timestamp()})


def map_info_view(request):
    city_list = get_map_info()
    return JsonResponse({"err_code": 0, "city_list": city_list})


@csrf_exempt
def attack_view(request):
    """
    a1: 进攻方兵力和
    a2: 进攻方武将战斗力和
    d1: 防守方兵力和
    d2: 防守方武将战斗力和
    d3: 防守方所在城市的城防

    进攻方战斗力： a1 * a2 / (a2 + d2)
    防守方战斗力：(d1+d3) * d2 / (a2 + d2)
    根据战斗力比率 roll 点，如果进攻方获胜，防守方兵力 -1k，城防 -1k。
    防守方获胜，进攻方兵力 -1k。
    直到有一方兵力为 0，或者防守方城防为 0。

    胜:
        => 占领 增加兵力增长速度 城池实力增长 损失兵力
    负:
        => 损失兵力
    """
    address = request.session.get("uid", "")
    city_id = int(request.POST.get("city_id"))
    if get_current_battle_state() != BattleState.battle:
        return JsonResponse({"err_code": -1, "msg": "当前不是战斗阶段"})
    city_ownership = CityOwnership.objects.filter(city_id=city_id).first()
    if not city_ownership:
        # 去掉城防 换所有权
        city_ownership = CityOwnership(address=address, city_id=city_id)
        city_ownership.save()
        return JsonResponse({"err_code": 0, "success": 1})

    if address == city_ownership.address:
        return JsonResponse({"err_code": -1, "msg": "自己不能进攻自己"})
    # 战斗 更换城市所有权
    city_ownership.address = address
    city_ownership.save()
    return {
        "err_code": 0,
        "success": 1,
    }
