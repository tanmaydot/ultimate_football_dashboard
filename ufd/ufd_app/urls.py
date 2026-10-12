from django.urls import path
from django.views.generic import RedirectView

from . import dashboard, team_pages

app_name = "ufd_app"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="ufd_app:dashboard_home"), name="home"),
    path("dashboard/", dashboard.dashboard_home, name="dashboard_home"),
    path("dashboard/<int:team_id>/", dashboard.dashboard_view, name="dashboard"),
    path("dashboard/<int:team_id>/squad/", team_pages.squad_view, name="squad"),
    path("dashboard/<int:team_id>/manager/", team_pages.manager_view, name="manager"),
    path("dashboard/<int:team_id>/formation/", team_pages.formation_view, name="formation"),
    path("dashboard/<int:team_id>/player/<int:player_id>/", team_pages.player_view, name="player"),
]
