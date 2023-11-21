from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import UnexpectedAlertPresentException
from bs4 import BeautifulSoup

p=r'C:\Users\USER\AppData\Local\Google\Chrome\User Data_2'
options = webdriver.ChromeOptions()
options.add_argument('--user-data-dir='+p) #Path to your chrome profile
#options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
# options.add_argument('--log-level=3')
# options.add_experimental_option('excludeSwitches', ['enable-logging'])
driver = webdriver.Chrome(options=options)

filename = "全球邁入神話時代.txt"                                # user add novel name here
first_page_url = 'https://uukanshu.cc/book/10498/6166272.html'      # user add the url of the first page
total_pages = 677                                                # user add total number of pages

driver.get(first_page_url)

soup = BeautifulSoup(driver.page_source, 'html.parser',)

for i in range(0,total_pages):
    # print(i)
    try:
        # print("Get title.")
        title = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, '/html/body/div[2]/div[1]/div/h1')) # title of every page
        )
        
        content = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, '/html/body/div[2]/div[1]/div/p[2]')) # content of every page
        )
        # button = WebDriverWait(driver, 3).until(
        #     EC.presence_of_element_located((By.XPATH, '//*[@id="linkNext"]')) # button of next page
        # )
        print(title.text, i)
        if i == 0 : # first page
            with open(filename, mode="w", encoding="utf-8") as file:
                file.write(title.text+"\n"+"\n")
                file.write(content.text+"\n"+"\n")
            next = driver.find_element(By.XPATH, '//*[@id="linkNext"]') # button of next page
            next.click()
        elif i == total_pages-1 : # last page
            with open(filename, mode="a", encoding="utf-8") as file:
                file.write(title.text+"\n"+"\n")
                file.write(content.text+"\n"+"\n")
        else: # between first and last pages
            with open(filename, mode="a", encoding="utf-8") as file:
                file.write(title.text+"\n"+"\n")
                file.write(content.text+"\n"+"\n")
            next = driver.find_element(By.XPATH, '//*[@id="linkNext"]') # button of next page
            next.click()
            
             
    except UnexpectedAlertPresentException as exception:
        print("fail")
        break   
        
print("Progam End!")
driver.close()