import numpy as np
import os, sys
import requests
import time

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Max
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template import loader
from django.views.decorators.csrf import csrf_protect

from website.utils import json_response

sys.path.append(os.path.join(
    os.path.dirname(__file__), "..", "tellina_learning_module"))

from bashlint import data_tools

WEBSITE_DEVELOP = False
CACHE_TRANSLATIONS = True

from website import functions
from website.cmd2html import tokens2html
from website.models import NL, Command, NLRequest, URL, Translation, Vote, User
from website.utils import get_tag, get_nl, get_command, NUM_TRANSLATIONS


if not WEBSITE_DEVELOP:
    from website.backend_interface import translate_fun


def ip_address_required(f):
    @functions.wraps(f)
    def g(request, *args, **kwargs):
        try:
            ip_address = request.COOKIES['ip_address']
        except KeyError:
            # redirect to home page if no ip address is captured
            return index(request)
        return f(request, *args, ip_address=ip_address, **kwargs)
    return g


@csrf_protect
def translate(request, ip_address='123.456.789.012'):
    template = loader.get_template('translator/translate.html')
    if request.method == 'POST':
        request_str = request.POST.get('request_str')
    else:
        request_str = request.GET.get('request_str')

    if not request_str or not request_str.strip():
        return redirect('/')
    
    while request_str.endswith('/'):
        request_str = request_str[:-1]

    # check if the natural language request is in the database
    nl = get_nl(request_str)

    trans_list = []
    annotated_trans_list = []    

    if CACHE_TRANSLATIONS and \
            Translation.objects.filter(nl=nl).exists():
        # model translations exist
        cached_trans = Translation.objects.filter(nl=nl).order_by('score')
        count = 0
        for trans in cached_trans:
            pred_tree = data_tools.bash_parser(trans.pred_cmd.str)
            if pred_tree is not None:
                trans_list.append(trans)
                annotated_trans_list.append(tokens2html(pred_tree))
            count += 1
            if count >= NUM_TRANSLATIONS:
                break

    # check if the user is in the database
    try:
        user = User.objects.get(ip_address=ip_address)
    except ObjectDoesNotExist:
        if ip_address == '123.456.789.012':
            organization = ''
            city = '--'
            region = '--'
            country = '--'
        else:
            r = requests.get('http://ipinfo.io/{}/json'.format(ip_address))
            organization = '' if r.json()['org'] is None else r.json()['org']
            city = '--' if r.json()['city'] is None else r.json()['city']
            region = '--' if r.json()['region'] is None else r.json()['region']
            country = '--' if r.json()['country'] is None else r.json()['country']
        
        user = User.objects.create(
            ip_address=ip_address,
            organization=organization,
            city=city,
            region=region,
            country=country
        )

    # save the natural language request issued by this IP Address
    nl_request = NLRequest.objects.create(nl=nl, user=user)

    start_time = time.time()
    if not trans_list:
        if not WEBSITE_DEVELOP:
            # call learning model and store the translations
            batch_outputs, output_logits = translate_fun(request_str)

            if batch_outputs:
                top_k_predictions = batch_outputs[0]
                top_k_scores = output_logits[0]

                for i in range(len(top_k_predictions)):
                    pred_tree, pred_cmd = top_k_predictions[i]
                    score = top_k_scores[i]
                    cmd = get_command(pred_cmd)
                    trans_set = Translation.objects.filter(nl=nl, pred_cmd=cmd)
                    if not trans_set.exists():
                        trans = Translation.objects.create(
                            nl=nl, pred_cmd=cmd, score=score)
                    else:
                        for trans in trans_set:
                            break
                        trans.score = score
                        trans.save()
                    trans_list.append(trans)
                    annotated_trans_list.append(tokens2html(pred_tree))
        
    translation_list = []
    for trans, annotated_cmd in zip(trans_list, annotated_trans_list):
        upvoted, downvoted, starred = "", "", ""
        if Vote.objects.filter(translation=trans, ip_address=ip_address).exists():
            v = Vote.objects.get(translation=trans, ip_address=ip_address)
            upvoted = 1 if v.upvoted else ""
            downvoted = 1 if v.downvoted else ""
            starred = 1 if v.starred else ""
        translation_list.append((trans, upvoted, downvoted, starred,
            trans.pred_cmd.str.replace('\\', '\\\\'), annotated_cmd))

    # sort translation_list based on voting results
    translation_list.sort(
        key=lambda x: x[0].num_votes + x[0].score, reverse=True)
    context = {
        'nl_request': nl_request,
        'trans_list': translation_list
    }
    print('backend translation time = {}'.format(time.time() - start_time))
    return HttpResponse(template.render(context, request))

@ip_address_required
def vote(request, ip_address):
    id = request.GET['id']
    upvoted = request.GET['upvoted']
    downvoted = request.GET['downvoted']
    starred = request.GET['starred']

    translation = Translation.objects.get(id=id)

    # store voting record in the DB
    if Vote.objects.filter(
            translation=translation, ip_address=ip_address).exists():
        vote = Vote.objects.get(translation=translation, ip_address=ip_address)
        if upvoted == 'true' and not vote.upvoted:
            translation.num_upvotes += 1
        if downvoted == 'true' and not vote.downvoted:
            translation.num_downvotes += 1
        if starred == 'true' and not vote.starred:
            translation.num_stars += 1
        if upvoted == 'false' and vote.upvoted:
            translation.num_upvotes -= 1
        if downvoted == 'false' and vote.downvoted:
            translation.num_downvotes -= 1
        if starred == 'false' and vote.starred:
            translation.num_stars -= 1
        vote.upvoted = (upvoted == 'true')
        vote.downvoted = (downvoted == 'true')
        vote.starred = (starred == 'true')
        vote.save()
    else:
        Vote.objects.create(
            translation=translation, ip_address=ip_address,
            upvoted=(upvoted == 'true'),
            downvoted=(downvoted == 'true'),
            starred=(starred == 'true')
        )
        if upvoted == 'true':
            translation.num_upvotes += 1
        if downvoted == 'true':
            translation.num_downvotes += 1
        if starred == 'true':
            translation.num_stars += 1
    translation.save()

    return HttpResponse()

def remember_ip_address(request):
    ip_address = request.GET['ip_address']
    resp = HttpResponse()
    resp.set_cookie('ip_address', ip_address)
    return resp

def index(request):
    template = loader.get_template('translator/index.html')
    return HttpResponse(template.render({}, request))

def example_requests_with_translations(request):
    example_requests_with_translations = []
    example_request_list = [
        'remove all pdfs in my current directory',
        'delete all *.txt files in "myDir/"',
        'list files in "myDir/" that have been modified within 24 hours',
        'find all files named "test*.cpp" and move them to "project/code/"',
        'find all files larger than a gigabyte in the current folder',
        'find all png files larger than 50M that were last modified more than 30 days ago'
    ]

    for request_str in example_request_list:
        nl = get_nl(request_str)
        if Translation.objects.filter(nl__str=request_str).exists():
            translations = Translation.objects.filter(nl__str=request_str)
            max_score = translations.aggregate(Max('score'))['score__max']
            for top_translation in Translation.objects.filter(
                    nl__str=request_str, score=max_score):
                break
        else:
            # Compute the translations on the fly
            top_translation = None
            if not WEBSITE_DEVELOP:
                # call learning model and store the translations
                batch_outputs, output_logits = translate_fun(request_str)
                max_score = -np.inf
                if batch_outputs:
                    top_k_predictions = batch_outputs[0]
                    top_k_scores = output_logits[0]
                    for i in range(len(top_k_predictions)):
                        pred_tree, pred_cmd = top_k_predictions[i]
                        score = top_k_scores[i]
                        if score > max_score:
                            max_score = score
                        cmd = get_command(pred_cmd)
                        top_translation = Translation.objects.create(
                            nl=nl, pred_cmd=cmd, score=score)
        if top_translation:
            example_requests_with_translations.append({
                'nl': nl.str,
                'top_translation': top_translation.pred_cmd.str,
                'tags': [tag.str for tag in top_translation.pred_cmd.tags.all().order_by('frequency')]
            })
        else:
            example_requests_with_translations.append({
                'nl': nl.str,
                'top_translation': 'No translation available.',
                'tags': []
            })

    return json_response({
        'example_requests_with_translations': example_requests_with_translations})

def latest_requests_with_translations(request):
    latest_requests_with_translations = []
    max_num_translation = 0

    for request in NLRequest.objects.order_by('-submission_time'):
        translations = Translation.objects.filter(nl=request.nl)
        top_translation = None
        if translations:
            max_score = translations.aggregate(Max('score'))['score__max']
            for top_translation in Translation.objects.filter(
                    nl=request.nl, score=max_score):
                break
        top_translation_tags = [tag.str for tag in top_translation.pred_cmd.tags.all().order_by('frequency')] \
            if top_translation else []
        top_translation_cmd = top_translation.pred_cmd.str if top_translation \
            else 'No translation available.'
        latest_requests_with_translations.append({
            'nl': request.nl.str, 
            'tags': top_translation_tags,
            'top_translation': top_translation_cmd,
            'submission_time': request.submission_time.strftime("%Y-%m-%d %H:%M:%S"),
            'user_city': request.user.city,
            'user_region': request.user.region,
            'user_country': request.user.country
        })
        max_num_translation += 1
        if max_num_translation % 20 == 0:
            break

    return json_response({
        'latest_requests_with_translations': latest_requests_with_translations})

def developers(request):
    template = loader.get_template('translator/developers.html')
    context = {}
    return HttpResponse(template.render(context, request))

