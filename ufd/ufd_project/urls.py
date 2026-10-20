from django.urls import include, path

urlpatterns = [
    path('', include('ufd_app.urls', namespace='ufd_app')),
]
