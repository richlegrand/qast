FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/site/
COPY docs/ /usr/share/nginx/html/docs/
COPY admin/ /usr/share/nginx/html/admin/
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
