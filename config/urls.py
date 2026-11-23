from django.urls import path

from diagrama import views

urlpatterns = [
    path("", views.DiagramaView.as_view(), name="diagrama"),
]
